from __future__ import annotations

from typing import Any, Callable, Optional

from app.core.classifier import MODE_TO_CATEGORY
from app.core.engine import scan_drives
from app.core.models import ScanItem

ProgressCb = Callable[[str, int, int, int], None]
CancelCb = Callable[[], bool]


def _run_mode(
    mode: str,
    roots: list[str],
    *,
    options: Optional[dict[str, Any]] = None,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
) -> list[ScanItem]:
    if mode not in MODE_TO_CATEGORY:
        raise ValueError(f"Unknown scan mode: {mode}")

    opts = dict(options or {})

    def _prog(path: str, seen: int, found: int, _drv: str, _spd: float, _cache: bool) -> None:
        if on_progress:
            on_progress(path, seen, found, 0)

    results = scan_drives(
        roots,
        [mode],
        opts,
        use_cache=bool(opts.get("use_cache", False)),
        force=not bool(opts.get("use_cache", False)),
        on_progress=_prog,
        should_cancel=should_cancel,
    )
    items: list[ScanItem] = []
    for res in results:
        items.extend(res.items)
    return items


def scan_large_files(
    roots: list[str],
    *,
    options: Optional[dict[str, Any]] = None,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
) -> list[ScanItem]:
    return _run_mode("large_files", roots, options=options, on_progress=on_progress, should_cancel=should_cancel)


def scan_duplicates(
    roots: list[str],
    *,
    options: Optional[dict[str, Any]] = None,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
) -> list[ScanItem]:
    return _run_mode("duplicates", roots, options=options, on_progress=on_progress, should_cancel=should_cancel)


def scan_temp(
    roots: list[str],
    *,
    options: Optional[dict[str, Any]] = None,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
) -> list[ScanItem]:
    return _run_mode("temp", roots, options=options, on_progress=on_progress, should_cancel=should_cancel)


def scan_old_files(
    roots: list[str],
    *,
    options: Optional[dict[str, Any]] = None,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
) -> list[ScanItem]:
    return _run_mode("old_files", roots, options=options, on_progress=on_progress, should_cancel=should_cancel)


def scan_empty_dirs(
    roots: list[str],
    *,
    options: Optional[dict[str, Any]] = None,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
) -> list[ScanItem]:
    return _run_mode("empty_dirs", roots, options=options, on_progress=on_progress, should_cancel=should_cancel)


def scan_old_downloads(
    roots: list[str],
    *,
    options: Optional[dict[str, Any]] = None,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
) -> list[ScanItem]:
    return _run_mode("old_downloads", roots, options=options, on_progress=on_progress, should_cancel=should_cancel)


MODES = {
    "large_files": scan_large_files,
    "duplicates": scan_duplicates,
    "temp": scan_temp,
    "old_files": scan_old_files,
    "empty_dirs": scan_empty_dirs,
    "old_downloads": scan_old_downloads,
}


def run_scan(
    mode: str,
    roots: list[str],
    options: Optional[dict[str, Any]] = None,
    *,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
) -> list[ScanItem]:
    return _run_mode(mode, roots, options=options, on_progress=on_progress, should_cancel=should_cancel)


__all__ = [
    "MODES",
    "run_scan",
    "scan_duplicates",
    "scan_empty_dirs",
    "scan_large_files",
    "scan_old_downloads",
    "scan_old_files",
    "scan_temp",
]
