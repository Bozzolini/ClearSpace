from __future__ import annotations

import time
from typing import Any, Callable, Optional

from app.core.cache import (
    load_drive_index,
    load_drive_result,
    record_scan_history,
    save_drive_index,
    save_drive_result,
    validate_item,
)
from app.core.classifier import ALL_MODES, classify_and_filter
from app.core.fast_walk import walk_drive_parallel
from app.core.models import DriveScanResult, FileRecord, ScanItem, ScanParameters

ProgressCb = Callable[[str, int, int, str, float, bool], None]
CancelCb = Callable[[], bool]
PauseCb = Callable[[], None]


def classify_records(
    records: list[FileRecord],
    modes: list[str],
    options: Optional[dict[str, Any]] = None,
    *,
    should_cancel: Optional[CancelCb] = None,
    wait_if_paused: Optional[PauseCb] = None,
    on_progress: Optional[ProgressCb] = None,
) -> list[ScanItem]:
    """Classify and strictly filter FileRecords according to selected modes & thresholds."""
    params = ScanParameters.from_options(drives=(), modes=modes, options=options or {})
    return classify_and_filter(
        records,
        params,
        should_cancel=should_cancel,
        wait_if_paused=wait_if_paused,
        on_progress=on_progress,
    )


def scan_drive(
    drive: str,
    modes: list[str],
    options: Optional[dict[str, Any]] = None,
    *,
    use_cache: bool = True,
    force: bool = False,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
    wait_if_paused: Optional[PauseCb] = None,
) -> DriveScanResult:
    """Scan a single drive or directory root using the 5-stage pipeline:
    Discovery -> Metadata -> Classification -> Filtering -> Valid Results.
    """
    opts = options or {}
    params = ScanParameters.from_options(drives=[drive], modes=modes, options=opts)
    t0 = time.perf_counter()

    if not params.modes:
        return DriveScanResult(
            drive=drive,
            items=[],
            files_seen=0,
            scanned_at=time.time(),
            duration_sec=0.0,
            from_cache=False,
            modes=[],
            complete=True,
        )

    # 1. Exact filtered result cache hit
    if use_cache and not force:
        cached = load_drive_result(drive, list(params.modes), params.to_options_dict())
        if cached is not None:
            items, files_seen, scanned_at = cached
            if on_progress:
                on_progress(drive, files_seen, len(items), drive, 0.0, True)
            return DriveScanResult(
                drive=drive,
                items=items,
                files_seen=files_seen,
                bytes_indexed=sum(i.size for i in items),
                scanned_at=scanned_at,
                duration_sec=time.perf_counter() - t0,
                from_cache=True,
                modes=list(params.modes),
                complete=True,
            )

        # 2. Metadata index cache hit (re-filter with new thresholds/modes without re-walking disk)
        cached_idx = load_drive_index(
            drive, custom_exclusions=params.custom_exclusions
        )
        if cached_idx is not None:
            records, indexed_at = cached_idx
            items = classify_and_filter(
                records,
                params,
                should_cancel=should_cancel,
                wait_if_paused=wait_if_paused,
                on_progress=on_progress,
            )
            if should_cancel and should_cancel():
                return DriveScanResult(
                    drive=drive,
                    items=[],
                    files_seen=0,
                    complete=False,
                    modes=list(params.modes),
                )
            items = [it for it in items if validate_item(it)]
            for it in items:
                if not it.drive:
                    it.drive = drive
            files_seen = sum(1 for r in records if not r.is_dir)
            save_drive_result(
                drive,
                list(params.modes),
                params.to_options_dict(),
                items,
                files_seen,
                is_complete=True,
            )
            if on_progress:
                on_progress(drive, files_seen, len(items), drive, 0.0, True)
            return DriveScanResult(
                drive=drive,
                items=items,
                files_seen=files_seen,
                bytes_indexed=sum(r.size for r in records if not r.is_dir),
                scanned_at=indexed_at,
                duration_sec=time.perf_counter() - t0,
                from_cache=True,
                modes=list(params.modes),
                complete=True,
            )

    def _walk_progress(path: str, seen: int, drv: str, speed: float) -> None:
        if on_progress:
            on_progress(path, seen, 0, drv, speed, False)

    records = walk_drive_parallel(
        drive,
        workers=params.max_workers,
        custom_exclusions=params.custom_exclusions,
        on_progress=_walk_progress,
        should_cancel=should_cancel,
        wait_if_paused=wait_if_paused,
    )

    if should_cancel and should_cancel():
        return DriveScanResult(
            drive=drive,
            items=[],
            files_seen=sum(1 for r in records if not r.is_dir),
            scanned_at=time.time(),
            duration_sec=time.perf_counter() - t0,
            from_cache=False,
            modes=list(params.modes),
            complete=False,
        )

    items = classify_and_filter(
        records,
        params,
        should_cancel=should_cancel,
        wait_if_paused=wait_if_paused,
        on_progress=on_progress,
    )

    if should_cancel and should_cancel():
        return DriveScanResult(
            drive=drive,
            items=[],
            files_seen=sum(1 for r in records if not r.is_dir),
            scanned_at=time.time(),
            duration_sec=time.perf_counter() - t0,
            from_cache=False,
            modes=list(params.modes),
            complete=False,
        )

    for it in items:
        if not it.drive:
            it.drive = drive

    files_seen = sum(1 for r in records if not r.is_dir)
    bytes_indexed = sum(r.size for r in records if not r.is_dir)
    duration = time.perf_counter() - t0

    # Save index and filtered result only for completed scans
    if use_cache:
        save_drive_index(
            drive,
            records,
            custom_exclusions=params.custom_exclusions,
            is_complete=True,
        )
        save_drive_result(
            drive,
            list(params.modes),
            params.to_options_dict(),
            items,
            files_seen,
            is_complete=True,
        )

    if on_progress:
        speed = files_seen / max(duration, 1e-6)
        on_progress(drive, files_seen, len(items), drive, speed, False)

    return DriveScanResult(
        drive=drive,
        items=items,
        files_seen=files_seen,
        bytes_indexed=bytes_indexed,
        scanned_at=time.time(),
        duration_sec=duration,
        from_cache=False,
        modes=list(params.modes),
        complete=True,
    )


def scan_drives(
    drives: list[str],
    modes: list[str],
    options: Optional[dict[str, Any]] = None,
    *,
    use_cache: bool = True,
    force: bool = False,
    on_progress: Optional[ProgressCb] = None,
    on_drive_done: Optional[Callable[[DriveScanResult], None]] = None,
    should_cancel: Optional[CancelCb] = None,
    wait_if_paused: Optional[PauseCb] = None,
) -> list[DriveScanResult]:
    """Scan multiple drives, emitting cached results first and recording history."""
    opts = options or {}
    t0 = time.perf_counter()
    results: list[DriveScanResult] = []
    pending: list[str] = []

    # Drives with a valid filtered-result cache are emitted first so the UI fills instantly.
    params = ScanParameters.from_options(drives=drives, modes=modes, options=opts)
    for drive in drives:
        if should_cancel and should_cancel():
            break
        cached = (
            load_drive_result(drive, list(params.modes), params.to_options_dict())
            if use_cache and not force and params.modes
            else None
        )
        if cached is None:
            pending.append(drive)
            continue
        items, files_seen, scanned_at = cached
        if on_progress:
            on_progress(drive, files_seen, len(items), drive, 0.0, True)
        res = DriveScanResult(
            drive=drive,
            items=items,
            files_seen=files_seen,
            bytes_indexed=sum(i.size for i in items),
            scanned_at=scanned_at,
            duration_sec=0.0,
            from_cache=True,
            modes=list(params.modes),
            complete=True,
        )
        results.append(res)
        if on_drive_done:
            on_drive_done(res)

    for drive in pending:
        if should_cancel and should_cancel():
            break
        res = scan_drive(
            drive,
            modes,
            opts,
            use_cache=use_cache,
            force=force,
            on_progress=on_progress,
            should_cancel=should_cancel,
            wait_if_paused=wait_if_paused,
        )
        if res.complete:
            results.append(res)
            if on_drive_done:
                on_drive_done(res)

    duration = time.perf_counter() - t0
    cancelled = bool(should_cancel and should_cancel())
    total_seen = sum(r.files_seen for r in results)
    total_items = sum(len(r.items) for r in results)
    total_bytes = sum(sum(i.size for i in r.items) for r in results)

    record_scan_history(
        drives=drives,
        modes=modes,
        files_seen=total_seen,
        items_found=total_items,
        potential_bytes=total_bytes,
        duration_sec=duration,
        status="CANCELLED" if cancelled else "COMPLETED",
    )

    return results


__all__ = ["ALL_MODES", "classify_records", "scan_drive", "scan_drives"]
