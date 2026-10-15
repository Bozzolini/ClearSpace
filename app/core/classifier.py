from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional, Sequence

from app.core.duplicates import find_duplicate_groups
from app.core.exclusions import (
    is_structural_folder_norm,
    is_under_excluded_norm,
    normalize_win_path,
    protected_prefixes,
)
from app.core.models import FileRecord, SafetyLevel, ScanItem, ScanParameters

MODE_TO_CATEGORY: dict[str, str] = {
    "large_files": "large_file",
    "duplicates": "duplicate",
    "temp": "temp_cache",
    "old_files": "old_file",
    "empty_dirs": "empty_dir",
    "old_downloads": "old_download",
}

CATEGORY_TO_MODE: dict[str, str] = {v: k for k, v in MODE_TO_CATEGORY.items()}

ALL_MODES: list[str] = list(MODE_TO_CATEGORY.keys())

# Unambiguous temporary/cache file extensions
TEMP_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".tmp",
        ".temp",
        ".dmp",
        ".hdmp",
        ".mdmp",
        ".chk",
        ".gid",
        "._mp",
        ".~mp",
        ".crdownload",
        ".part",
        ".partial",
        ".cache",
        ".etl",
    }
)

# Dedicated browser / OS cache directory names where cache blobs live
DEDICATED_CACHE_DIR_NAMES: frozenset[str] = frozenset(
    {
        "inetcache",
        "gpucache",
        "code cache",
        "shadercache",
        "dxcache",
        "cache2",
        "dawn_cache",
        "dawncache",
        "grshadercache",
        "crashpad",
    }
)

# Browser/app cache file prefixes when inside a 'cache' folder
CACHE_BLOB_PREFIXES: tuple[str, ...] = (
    "f_",
    "data_",
    "index",
    "thumbcache_",
    "iconcache_",
)

_CACHE_DIR_NAMES: frozenset[str] = frozenset({"cache", "caches", "npm-cache"})
_LOG_DIR_NAMES: frozenset[str] = frozenset({"temp", "tmp", "logs", "log"})
_DOWNLOAD_DIR_NAMES: frozenset[str] = frozenset({"downloads", "download"})


def is_recognized_temp_file(
    path: str,
    custom_temp_prefixes: Sequence[str] = (),
) -> bool:
    """Return True ONLY if `path` is a genuinely recognized temporary or cache file.

    Never classifies normal user/application files just because they sit in a scanned
    parent directory (such as AppData\\Local or a test root inside %TEMP%).
    """
    return _is_temp_norm(normalize_win_path(path), custom_temp_prefixes)


def _is_temp_norm(norm: str, custom_temp_prefixes: Sequence[str] = ()) -> bool:
    if not norm:
        return False
    sep = norm.rfind("\\")
    name = norm[sep + 1 :]
    if not name:
        return False

    _, ext = os.path.splitext(name)

    # 1. Explicit temporary/backup/partial extensions anywhere in the scanned tree
    if ext in TEMP_EXTENSIONS:
        return True

    # 2. Office/Windows temporary lock or tilde files (e.g. ~$document.docx, ~DF1234.tmp)
    if name.startswith("~$") or (name.startswith("~") and ext in {".tmp", ".temp", ".wrl"}):
        return True

    # 3. Windows Explorer thumbnail/icon cache databases
    if (name.startswith("thumbcache_") or name.startswith("iconcache_")) and ext == ".db":
        return True

    dir_parts = norm[:sep].split("\\") if sep > 0 else []

    # 4. Dedicated browser/system cache subdirectories (GPUCache, INetCache, Code Cache, cache2)
    if not DEDICATED_CACHE_DIR_NAMES.isdisjoint(dir_parts):
        return True

    # 5. Inside a directory explicitly named 'cache' or 'caches' with cache blob naming or .log/.dat
    if not _CACHE_DIR_NAMES.isdisjoint(dir_parts):
        if name.startswith(CACHE_BLOB_PREFIXES) or ext in {".log", ".dat", ".bin", ".cac"}:
            return True

    # 6. Log files inside a Temp/Tmp/Logs directory
    if ext == ".log" and not _LOG_DIR_NAMES.isdisjoint(dir_parts):
        return True

    # 7. User-configured explicit custom temp directories: still require temp/cache/log indicator
    if custom_temp_prefixes:
        for pref in custom_temp_prefixes:
            if norm == pref or norm.startswith(pref + "\\"):
                if ext in TEMP_EXTENSIONS or ext in {".log", ".dat", ".cache"} or name.startswith("~"):
                    return True

    return False


def get_download_prefixes(custom_dirs: Sequence[str] = ()) -> tuple[str, ...]:
    """Return normalized paths to recognized Downloads directories."""
    candidates: list[str] = list(custom_dirs)
    user = os.environ.get("USERPROFILE", "")
    if user:
        candidates.append(os.path.join(user, "Downloads"))
        candidates.append(os.path.join(user, "Download"))
    onedrive = os.environ.get("OneDrive", "")
    if onedrive:
        candidates.append(os.path.join(onedrive, "Downloads"))

    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if not c:
            continue
        norm = normalize_win_path(c)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return tuple(out)


def is_in_downloads(path: str, download_prefixes: Sequence[str]) -> bool:
    """Return True if `path` is inside a recognized Downloads directory.

    Recognized: the current user's (and OneDrive's) Downloads, configured download
    folders, `<drive>:\\Downloads`, and `<drive>:\\Users\\<name>\\Downloads`.
    A folder merely *named* "downloads" deep inside a project tree is not a Downloads directory.
    """
    return _in_downloads_norm(normalize_win_path(path), download_prefixes)


def _in_downloads_norm(norm: str, download_prefixes: Sequence[str]) -> bool:
    for pref in download_prefixes:
        if norm.startswith(pref + "\\"):
            return True
    parts = norm.split("\\")
    if len(parts) > 2 and parts[1] in _DOWNLOAD_DIR_NAMES:
        return True
    return len(parts) > 4 and parts[1] == "users" and parts[3] in _DOWNLOAD_DIR_NAMES


def verify_truly_empty_dir(path: str) -> bool:
    """Verify on disk that a directory exists, is readable, and contains 0 entries.

    Returns False if the directory has any file (including hidden/system files)
    or if PermissionError / OSError prevents reading it.
    """
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return False


def classify_and_filter(
    records: list[FileRecord],
    params: ScanParameters,
    *,
    now: Optional[float] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    wait_if_paused: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[str, int, int, str, float, bool], None]] = None,
) -> list[ScanItem]:
    """Pipeline stage: Classification -> Strict Filtering -> Valid ScanItems.

    Guarantees:
    - Only modes present in `params.modes` are evaluated.
    - Each returned ScanItem matches at least one selected mode.
    - A file matching multiple modes is emitted ONCE with all matching `categories`.
    - Duplicate groups are full-content verified and contain >= 2 identical files.
    """
    active_modes = [m for m in params.modes if m in MODE_TO_CATEGORY]
    if not active_modes:
        return []

    modes_set = set(active_modes)
    current_time = now if now is not None else time.time()

    large_threshold = int(params.min_mb * 1024 * 1024)
    dup_min_bytes = max(1, int(params.min_kb * 1024))
    cutoff_old = current_time - (params.days_old * 86400.0)
    cutoff_dl = current_time - (params.days_downloads * 86400.0)

    excl_prefixes = protected_prefixes(params.custom_exclusions)
    dl_prefixes = get_download_prefixes(params.custom_download_dirs) if "old_downloads" in modes_set else ()
    temp_prefixes = tuple(normalize_win_path(p) for p in params.custom_temp_dirs if p)

    # Map normalized path -> accumulated categories and record metadata
    by_path: dict[str, dict[str, Any]] = {}

    def _add_match(
        rec: FileRecord,
        category: str,
        *,
        group_id: Optional[str] = None,
        is_primary: bool = False,
        key: Optional[str] = None,
    ) -> None:
        if key is None:
            key = normalize_win_path(rec.path)
        entry = by_path.get(key)
        if entry is None:
            entry = {
                "rec": rec,
                "categories": [],
                "group_id": group_id,
                "is_primary": is_primary,
            }
            by_path[key] = entry
        if category not in entry["categories"]:
            entry["categories"].append(category)
        if group_id is not None:
            entry["group_id"] = group_id
            entry["is_primary"] = is_primary

    want_large = "large_files" in modes_set and large_threshold > 0
    want_temp = "temp" in modes_set
    want_old = "old_files" in modes_set
    want_dl = "old_downloads" in modes_set
    want_empty = "empty_dirs" in modes_set
    want_dups = "duplicates" in modes_set
    dup_candidates: list[FileRecord] = []

    for n, rec in enumerate(records):
        if n & 0x3FF == 0:
            if should_cancel and should_cancel():
                return []
            if wait_if_paused:
                wait_if_paused()

        norm = normalize_win_path(rec.path)
        if is_under_excluded_norm(norm, excl_prefixes):
            continue

        if rec.is_dir:
            if (
                want_empty
                and rec.is_empty_dir
                and not is_structural_folder_norm(norm)
                and verify_truly_empty_dir(rec.path)
            ):
                _add_match(rec, "empty_dir", key=norm)
            continue

        if want_dups and rec.size >= dup_min_bytes:
            dup_candidates.append(rec)

        # 1. Large files: strictly >= configured threshold (and > 0 bytes)
        if want_large and rec.size > 0 and rec.size >= large_threshold:
            _add_match(rec, "large_file", key=norm)

        # 2. Temporary / cache files: only genuinely recognized temp/cache files
        if want_temp and _is_temp_norm(norm, temp_prefixes):
            _add_match(rec, "temp_cache", key=norm)

        # 3. Old files: last modification (st_mtime) strictly older than cutoff_old
        if want_old and 0 < rec.mtime < cutoff_old:
            _add_match(rec, "old_file", key=norm)

        # 4. Old downloads: inside a recognized Downloads directory AND mtime older than cutoff_dl
        if want_dl and 0 < rec.mtime < cutoff_dl and _in_downloads_norm(norm, dl_prefixes):
            _add_match(rec, "old_download", key=norm)

    # 5. Duplicates: progressive size -> partial hash -> FULL hash verification
    if want_dups and not (should_cancel and should_cancel()):
        non_excluded_files = dup_candidates

        def _dup_progress(path: str, done_count: int) -> None:
            if on_progress:
                on_progress(path, done_count, len(by_path), "", 0.0, False)

        dup_groups = find_duplicate_groups(
            non_excluded_files,
            min_size_bytes=dup_min_bytes,
            max_workers=params.max_workers,
            should_cancel=should_cancel,
            wait_if_paused=wait_if_paused,
            on_progress=_dup_progress,
        )
        for gid, group in dup_groups:
            if len(group) < 2:
                continue
            for idx, rec in enumerate(group):
                _add_match(rec, "duplicate", group_id=gid, is_primary=(idx == 0))

    if should_cancel and should_cancel():
        return []

    # Filtering & Validation stage: ensure every item strictly belongs to selected modes
    allowed_categories = {MODE_TO_CATEGORY[m] for m in active_modes}
    results: list[ScanItem] = []

    for entry in by_path.values():
        rec: FileRecord = entry["rec"]
        cats = tuple(c for c in entry["categories"] if c in allowed_categories)
        if not cats:
            continue

        group_id: Optional[str] = entry["group_id"]
        is_primary: bool = bool(entry["is_primary"])

        # Determine safety classification:
        # - Primary copy of a duplicate group is always CANDIDATE (keep by default)
        # - Temp/cache, empty_dir, and non-primary duplicate copies are SAFE
        # - Large files, old files, old downloads are CANDIDATE (user should review)
        if is_primary and "duplicate" in cats:
            safety = SafetyLevel.CANDIDATE.value
        elif any(c in {"temp_cache", "empty_dir"} for c in cats) or (
            "duplicate" in cats and not is_primary
        ):
            safety = SafetyLevel.SAFE.value
        else:
            safety = SafetyLevel.CANDIDATE.value

        results.append(
            ScanItem(
                path=rec.path,
                size=0 if rec.is_dir else rec.size,
                mtime=rec.mtime,
                reason=cats[0],
                categories=cats,
                drive=rec.drive,
                group_id=group_id,
                is_primary_in_group=is_primary,
                safety=safety,
                selected=False,
            )
        )

    results.sort(key=lambda i: (i.size, i.mtime), reverse=True)
    return results
