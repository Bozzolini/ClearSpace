from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


def _finite_nonneg(value: Any, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0.0, f) if math.isfinite(f) else default


class ScanState(str, Enum):
    IDLE = "IDLE"
    SCANNING = "SCANNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class SafetyLevel(str, Enum):
    SAFE = "safe"          # Temp/cache files, empty dirs, redundant duplicate copies
    CANDIDATE = "candidate"  # Large/old/download files, or primary duplicate copy


@dataclass(frozen=True)
class ScanParameters:
    """Immutable snapshot of scan configuration to prevent race conditions."""

    drives: tuple[str, ...]
    modes: tuple[str, ...]
    min_mb: float = 100.0
    min_kb: float = 64.0
    days_old: float = 365.0
    days_downloads: float = 90.0
    custom_exclusions: tuple[str, ...] = ()
    use_cache: bool = True
    force_rescan: bool = False
    max_workers: int = 0
    custom_temp_dirs: tuple[str, ...] = ()
    custom_download_dirs: tuple[str, ...] = ()

    @classmethod
    def from_options(
        cls,
        drives: list[str] | tuple[str, ...],
        modes: list[str] | tuple[str, ...],
        options: dict[str, Any] | None = None,
    ) -> ScanParameters:
        opts = options or {}
        days_default = float(opts.get("days", 365.0))
        days_old = float(opts.get("days_old", days_default))
        if "days_downloads" in opts:
            days_dl = float(opts["days_downloads"])
        elif "old_downloads" in modes and "old_files" not in modes and "days" in opts:
            days_dl = float(opts["days"])
        else:
            days_dl = 90.0
        return cls(
            drives=tuple(drives),
            modes=tuple(modes),
            min_mb=_finite_nonneg(opts.get("min_mb", 100.0), 100.0),
            min_kb=_finite_nonneg(opts.get("min_kb", 64.0), 64.0),
            days_old=_finite_nonneg(days_old, 365.0),
            days_downloads=_finite_nonneg(days_dl, 90.0),
            custom_exclusions=tuple(opts.get("custom_exclusions", ())),
            use_cache=bool(opts.get("use_cache", True)),
            force_rescan=bool(opts.get("force_rescan", False)),
            max_workers=int(min(32.0, _finite_nonneg(opts.get("max_workers", 0), 0.0))),
            custom_temp_dirs=tuple(opts.get("custom_temp_dirs", ())),
            custom_download_dirs=tuple(opts.get("custom_download_dirs", ())),
        )

    def to_options_dict(self) -> dict[str, Any]:
        return {
            "min_mb": self.min_mb,
            "min_kb": self.min_kb,
            "days": self.days_old,
            "days_old": self.days_old,
            "days_downloads": self.days_downloads,
            "custom_exclusions": list(self.custom_exclusions),
            "custom_temp_dirs": list(self.custom_temp_dirs),
            "custom_download_dirs": list(self.custom_download_dirs),
        }


@dataclass(slots=True)
class FileRecord:
    path: str
    size: int
    mtime: float
    is_dir: bool
    drive: str
    is_empty_dir: bool = False
    attributes: int = 0

    @property
    def name(self) -> str:
        return os.path.basename(self.path.rstrip("\\/"))


@dataclass
class ScanItem:
    path: str
    size: int
    mtime: float
    reason: str
    drive: str = ""
    group_id: Optional[str] = None
    selected: bool = False
    categories: tuple[str, ...] = ()
    is_primary_in_group: bool = False
    safety: str = SafetyLevel.CANDIDATE.value
    status: str = "ready"

    def __post_init__(self) -> None:
        if not self.categories and self.reason:
            self.categories = (self.reason,)
        elif self.categories and not self.reason:
            self.reason = self.categories[0]

    @property
    def name(self) -> str:
        return os.path.basename(self.path.rstrip("\\/")) or self.path


@dataclass
class DriveInfo:
    root: str
    label: str = ""
    fs_type: str = "NTFS"
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    is_ssd: bool = True

    @property
    def used_ratio(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(1.0, max(0.0, self.used_bytes / self.total_bytes))


@dataclass
class ScanProgress:
    state: ScanState = ScanState.IDLE
    current_path: str = ""
    files_seen: int = 0
    items_found: int = 0
    bytes_found: int = 0
    drive: str = ""
    speed: float = 0.0
    elapsed_sec: float = 0.0
    done: bool = False
    cancelled: bool = False
    error: str = ""
    from_cache: bool = False


@dataclass
class DriveScanResult:
    drive: str
    items: list[ScanItem] = field(default_factory=list)
    files_seen: int = 0
    bytes_indexed: int = 0
    scanned_at: float = 0.0
    duration_sec: float = 0.0
    from_cache: bool = False
    modes: list[str] = field(default_factory=list)
    complete: bool = True


@dataclass
class ScanHistoryEntry:
    id: int
    timestamp: float
    drives: list[str]
    modes: list[str]
    files_seen: int
    items_found: int
    potential_bytes: int
    duration_sec: float
    status: str


@dataclass
class CleanupResult:
    trashed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped_changed: list[tuple[str, str]] = field(default_factory=list)
    bytes_sent: int = 0
