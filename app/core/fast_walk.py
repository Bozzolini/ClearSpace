from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, Sequence

from app.core.disk import (
    is_drive_root,
    is_reparse_or_symlink,
    path_is_reparse,
    recommended_workers,
    to_long_path,
)
from app.core.exclusions import (
    CRITICAL_SYSTEM_FILENAMES,
    PROTECTED_DIR_NAMES,
    build_exclusion_prefixes,
    is_under_excluded,
    normalize_win_path,
)
from app.core.models import FileRecord

ProgressCb = Callable[[str, int, str, float], None]  # path, seen, drive, files_per_sec
CancelCb = Callable[[], bool]
PauseCb = Callable[[], None]


def _scan_single_dir_non_recursive(
    dir_path: str,
    drive: str,
    excl_prefixes: tuple[str, ...],
    is_root: bool,
) -> tuple[list[FileRecord], list[str]]:
    """Scan one directory level, returning (file_records_at_level, child_subdirs)."""
    files: list[FileRecord] = []
    subdirs: list[str] = []
    raw_count = 0

    try:
        with os.scandir(to_long_path(dir_path)) as it:
            for entry in it:
                raw_count += 1
                name_low = entry.name.lower()
                if is_reparse_or_symlink(entry):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue

                full_path = os.path.join(dir_path, entry.name)
                if is_dir:
                    if name_low in PROTECTED_DIR_NAMES:
                        continue
                    if is_under_excluded(full_path, excl_prefixes):
                        continue
                    subdirs.append(full_path)
                else:
                    if name_low in CRITICAL_SYSTEM_FILENAMES:
                        continue
                    if is_under_excluded(full_path, excl_prefixes):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    files.append(
                        FileRecord(
                            path=full_path,
                            size=st.st_size,
                            mtime=st.st_mtime,
                            is_dir=False,
                            drive=drive,
                            is_empty_dir=False,
                            attributes=getattr(st, "st_file_attributes", 0),
                        )
                    )
    except OSError:
        # Inaccessible directory: never mark as empty
        return files, subdirs

    if raw_count == 0 and not is_root:
        try:
            mtime = os.path.getmtime(to_long_path(dir_path))
        except OSError:
            mtime = 0.0
        files.append(
            FileRecord(
                path=dir_path,
                size=0,
                mtime=mtime,
                is_dir=True,
                drive=drive,
                is_empty_dir=True,
            )
        )

    return files, subdirs


def _walk_subtree(
    start: str,
    drive: str,
    exclusions: Sequence[str],
    should_cancel: Optional[CancelCb],
    local_counter: list[int],
    *,
    wait_if_paused: Optional[PauseCb] = None,
    on_batch_progress: Optional[Callable[[str, int], None]] = None,
    is_root_call: bool = True,
) -> tuple[list[FileRecord], list[str]]:
    """Fast iterative os.scandir walk with zero Path.resolve() overhead."""
    excl_prefixes = (
        exclusions
        if isinstance(exclusions, tuple)
        else build_exclusion_prefixes(exclusions)
    )
    if is_under_excluded(start, excl_prefixes):
        return [], []

    files: list[FileRecord] = []
    empty_dirs: list[str] = []
    start_norm = normalize_win_path(start)
    drive_norm = normalize_win_path(drive)
    stack: list[str] = [os.path.abspath(start)]
    batch_seen = 0

    while stack:
        if should_cancel and should_cancel():
            break
        if wait_if_paused:
            wait_if_paused()

        current = stack.pop()
        raw_count = 0

        try:
            with os.scandir(to_long_path(current)) as it:
                for entry in it:
                    raw_count += 1
                    if should_cancel and should_cancel():
                        break
                    name_low = entry.name.lower()
                    if is_reparse_or_symlink(entry):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue

                    full_path = os.path.join(current, entry.name)
                    if is_dir:
                        if name_low in PROTECTED_DIR_NAMES:
                            continue
                        if is_under_excluded(full_path, excl_prefixes):
                            continue
                        stack.append(full_path)
                    else:
                        if name_low in CRITICAL_SYSTEM_FILENAMES:
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        files.append(
                            FileRecord(
                                path=full_path,
                                size=st.st_size,
                                mtime=st.st_mtime,
                                is_dir=False,
                                drive=drive,
                                is_empty_dir=False,
                                attributes=getattr(st, "st_file_attributes", 0),
                            )
                        )
                        local_counter[0] += 1
                        batch_seen += 1
                        if on_batch_progress and batch_seen >= 512:
                            on_batch_progress(full_path, batch_seen)
                            batch_seen = 0
        except OSError:
            continue

        # Only genuinely empty readable directories (not the scan root itself)
        if raw_count == 0:
            cur_norm = normalize_win_path(current)
            if cur_norm != drive_norm and not (is_root_call and cur_norm == start_norm):
                try:
                    mtime = os.path.getmtime(to_long_path(current))
                except OSError:
                    mtime = 0.0
                empty_dirs.append(current)
                files.append(
                    FileRecord(
                        path=current,
                        size=0,
                        mtime=mtime,
                        is_dir=True,
                        drive=drive,
                        is_empty_dir=True,
                    )
                )

    if on_batch_progress and batch_seen > 0:
        sample = files[-1].path if files else start
        on_batch_progress(sample, batch_seen)

    return files, empty_dirs


def walk_drive_parallel(
    drive: str,
    *,
    workers: int = 0,
    custom_exclusions: Sequence[str] = (),
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
    wait_if_paused: Optional[PauseCb] = None,
) -> list[FileRecord]:
    """Walk a drive or root directory using 2-level work partitioning and throttled callbacks."""
    from app.core.exclusions import default_exclusions

    excl_list = default_exclusions(custom_exclusions)
    excl_prefixes = build_exclusion_prefixes(excl_list)
    root_abs = os.path.abspath(drive)

    if is_under_excluded(root_abs, excl_prefixes) or not os.path.exists(to_long_path(root_abs)):
        return []
    # A custom folder that is a junction/symlink can point at Windows or another user.
    # Drive letters that are mount points are allowed (that's how extra volumes appear).
    if not is_drive_root(root_abs) and path_is_reparse(root_abs):
        return []

    t0 = time.perf_counter()

    # Level 1 & Level 2 partitioning to prevent a single huge directory (like C:\Users)
    # from bottlenecking a single worker thread.
    root_files, level1_dirs = _scan_single_dir_non_recursive(
        root_abs, drive, excl_prefixes, is_root=True
    )
    all_records: list[FileRecord] = list(root_files)
    work_dirs: list[str] = []

    for d1 in level1_dirs:
        if should_cancel and should_cancel():
            return []
        f1, sub2 = _scan_single_dir_non_recursive(
            d1, drive, excl_prefixes, is_root=False
        )
        all_records.extend(f1)
        work_dirs.extend(sub2)

    if not work_dirs:
        if on_progress:
            elapsed = max(time.perf_counter() - t0, 1e-6)
            on_progress(root_abs, len(all_records), drive, len(all_records) / elapsed)
        return all_records

    if workers <= 0:
        workers = recommended_workers(root_abs)
    workers = min(workers, max(1, len(work_dirs)))

    lock = threading.Lock()
    total_seen = len(all_records)
    last_emit = [0.0]

    def _report_batch(sample_path: str, delta: int) -> None:
        nonlocal total_seen
        now = time.perf_counter()
        with lock:
            total_seen += delta
            if on_progress and (now - last_emit[0]) >= 0.08:
                last_emit[0] = now
                elapsed = max(now - t0, 1e-6)
                on_progress(sample_path, total_seen, drive, total_seen / elapsed)

    def _job(sub_root: str) -> list[FileRecord]:
        local = [0]
        recs, _ = _walk_subtree(
            sub_root,
            drive,
            excl_prefixes,
            should_cancel,
            local,
            wait_if_paused=wait_if_paused,
            on_batch_progress=_report_batch,
            is_root_call=False,
        )
        return recs

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cs-walk") as pool:
        futures = {pool.submit(_job, wd): wd for wd in work_dirs}
        for fut in as_completed(futures):
            if should_cancel and should_cancel():
                for f in futures:
                    f.cancel()
                break
            try:
                recs = fut.result()
            except Exception:  # noqa: BLE001
                continue
            all_records.extend(recs)

    if on_progress and not (should_cancel and should_cancel()):
        elapsed = max(time.perf_counter() - t0, 1e-6)
        sample = all_records[-1].path if all_records else root_abs
        on_progress(sample, len(all_records), drive, len(all_records) / elapsed)

    return all_records
