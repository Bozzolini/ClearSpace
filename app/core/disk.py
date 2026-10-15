from __future__ import annotations

import ctypes
import os
import shutil
import string
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

from app.core.exclusions import (
    build_exclusion_prefixes,
    default_exclusions,
    is_under_excluded,
)
from app.core.models import DriveInfo

ProgressCb = Callable[[str, int], None]
CancelCb = Callable[[], bool]

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def list_drives() -> list[str]:
    """Return available Windows drive roots like ['C:\\', 'D:\\']."""
    drives: list[str] = []
    if os.name == "nt":
        try:
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i, letter in enumerate(string.ascii_uppercase):
                if bitmask & (1 << i):
                    root = f"{letter}:\\"
                    if os.path.exists(root):
                        drives.append(root)
            if drives:
                return drives
        except Exception:  # noqa: BLE001
            pass

    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append(root)
    return drives


def _get_volume_meta(root: str) -> tuple[str, str]:
    """Return (volume_label, fs_type) on Windows."""
    if os.name != "nt":
        return ("", "NTFS")
    try:
        vol_buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            vol_buf,
            len(vol_buf),
            None,
            None,
            None,
            fs_buf,
            len(fs_buf),
        )
        if ok:
            return (vol_buf.value or "", fs_buf.value or "NTFS")
    except Exception:  # noqa: BLE001
        pass
    return ("", "NTFS")


DRIVE_REMOVABLE = 2
DRIVE_REMOTE = 4


class _StoragePropertyQuery(ctypes.Structure):
    _fields_ = [
        ("PropertyId", ctypes.c_uint),
        ("QueryType", ctypes.c_uint),
        ("AdditionalParameters", ctypes.c_ubyte * 1),
    ]


class _SeekPenaltyDescriptor(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_uint),
        ("Size", ctypes.c_uint),
        ("IncursSeekPenalty", ctypes.c_ubyte),
    ]


def _drive_letter_root(path: str) -> str:
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    return (drive.upper() + "\\") if drive else ""


@lru_cache(maxsize=32)
def has_seek_penalty(root: str) -> Optional[bool]:
    """True for rotational disks (HDD), False for SSD/NVMe, None when Windows can't tell.

    Uses IOCTL_STORAGE_QUERY_PROPERTY / StorageDeviceSeekPenaltyProperty, which works
    without administrator rights (the volume is opened with zero access).
    """
    if os.name != "nt":
        return None
    letter_root = _drive_letter_root(root)
    if not letter_root:
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        f"\\\\.\\{letter_root[:2]}", 0, 0x1 | 0x2, None, 3, 0, None
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        return None
    try:
        query = _StoragePropertyQuery(7, 0)  # StorageDeviceSeekPenaltyProperty, PropertyStandardQuery
        desc = _SeekPenaltyDescriptor()
        returned = ctypes.c_uint(0)
        ok = kernel32.DeviceIoControl(
            ctypes.c_void_p(handle),
            0x002D1400,  # IOCTL_STORAGE_QUERY_PROPERTY
            ctypes.byref(query),
            ctypes.sizeof(query),
            ctypes.byref(desc),
            ctypes.sizeof(desc),
            ctypes.byref(returned),
            None,
        )
        if not ok or returned.value < ctypes.sizeof(desc):
            return None
        return bool(desc.IncursSeekPenalty)
    except Exception:  # noqa: BLE001
        return None
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


@lru_cache(maxsize=32)
def recommended_workers(path: str) -> int:
    """I/O worker count tuned to the storage under `path`.

    HDDs degrade badly under parallel random reads (head seeks), so they get 2 workers;
    network shares get a moderate 4; SSD/NVMe get up to 16.
    """
    cpu = os.cpu_count() or 4
    ssd_workers = min(16, max(4, cpu * 2))
    if os.name != "nt":
        return ssd_workers
    letter_root = _drive_letter_root(path)
    if not letter_root:
        return 4  # UNC path
    try:
        kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(letter_root))
    except Exception:  # noqa: BLE001
        kind = 0
    if kind == DRIVE_REMOTE:
        return 4
    if kind == DRIVE_REMOVABLE:
        return 2
    penalty = has_seek_penalty(letter_root)
    if penalty:
        return 2
    return ssd_workers


def get_drive_details() -> list[DriveInfo]:
    """Return detailed usage and metadata for all mounted drives."""
    infos: list[DriveInfo] = []
    for root in list_drives():
        try:
            usage = shutil.disk_usage(root)
            label, fs_type = _get_volume_meta(root)
            infos.append(
                DriveInfo(
                    root=root,
                    label=label,
                    fs_type=fs_type or "NTFS",
                    total_bytes=usage.total,
                    used_bytes=usage.used,
                    free_bytes=usage.free,
                    is_ssd=has_seek_penalty(root) is not True,
                )
            )
        except OSError:
            continue
    return infos


def is_excluded(path: str, exclusions: Optional[Sequence[str]] = None) -> bool:
    """Fast boundary-safe check whether path is excluded."""
    prefixes = build_exclusion_prefixes(exclusions)
    return is_under_excluded(path, prefixes)


def path_is_reparse(path: str) -> bool:
    """True if `path` itself is a symlink, junction or mount point."""
    try:
        if os.path.islink(path):
            return True
        st = os.stat(path, follow_symlinks=False)
        return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True


def is_drive_root(path: str) -> bool:
    abs_p = os.path.abspath(path)
    drive, tail = os.path.splitdrive(abs_p)
    return bool(drive) and tail in ("\\", "/", "")


def is_reparse_or_symlink(entry: os.DirEntry[str]) -> bool:
    """Detect symlinks and Windows junctions/reparse points without extra syscalls."""
    try:
        if entry.is_symlink():
            return True
        st = entry.stat(follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    except OSError:
        return True
    return False


def to_long_path(path: str) -> str:
    """Enable Windows extended-length path prefix when path approaches MAX_PATH."""
    if os.name != "nt" or len(path) < 240 or path.startswith("\\\\?\\"):
        return path
    abspath = os.path.abspath(path)
    if abspath.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abspath[2:]
    return "\\\\?\\" + abspath


def iter_files(
    roots: Iterable[str],
    *,
    exclusions: Optional[Sequence[str]] = None,
    on_progress: Optional[ProgressCb] = None,
    should_cancel: Optional[CancelCb] = None,
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    """Walk roots yielding file paths, skipping excluded system folders and junctions."""
    prefixes = build_exclusion_prefixes(exclusions)
    seen = 0

    for root in roots:
        if not root or not os.path.exists(root):
            continue
        if is_under_excluded(root, prefixes):
            continue

        stack = [os.path.abspath(root)]
        while stack:
            if should_cancel and should_cancel():
                return
            current = stack.pop()
            if is_under_excluded(current, prefixes):
                continue
            try:
                with os.scandir(to_long_path(current)) as it:
                    entries = list(it)
            except OSError:
                continue

            for entry in entries:
                if should_cancel and should_cancel():
                    return
                if not follow_symlinks and is_reparse_or_symlink(entry):
                    continue
                full = os.path.join(current, entry.name)
                if is_under_excluded(full, prefixes):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=follow_symlinks):
                        stack.append(full)
                    else:
                        seen += 1
                        if on_progress and seen % 250 == 0:
                            on_progress(full, seen)
                        yield Path(full)
                except OSError:
                    continue


def format_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(max(0, num_bytes))
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"
