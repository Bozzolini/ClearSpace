from __future__ import annotations

import os
from typing import Iterable, Mapping, Optional, Sequence

from send2trash import send2trash

from app.core.cache import invalidate_drive_cache
from app.core.classifier import verify_truly_empty_dir
from app.core.disk import FILE_ATTRIBUTE_REPARSE_POINT
from app.core.duplicates import compute_full_hash, file_identity
from app.core.exclusions import (
    is_sensitive_norm,
    is_structural_folder_norm,
    is_under_excluded,
    normalize_win_path,
    protected_prefixes,
)
from app.core.privacy import basename_for_log
from app.core.logging_config import get_logger
from app.core.models import CleanupResult, ScanItem

log = get_logger("clearspace.trash")


def _is_reparse(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        st = os.stat(path, follow_symlinks=False)
        return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True


def _crosses_link(path: str, memo: dict[str, bool]) -> bool:
    """True if any parent folder is a junction / symlink / mount point.

    Such a path resolves somewhere other than where it appears to be (possibly inside
    a protected location), so the lexical exclusion checks cannot be trusted for it.
    """
    parent = os.path.dirname(os.path.abspath(path))
    chain: list[str] = []
    while parent and parent not in memo:
        nxt = os.path.dirname(parent)
        if nxt == parent:  # drive root
            memo[parent] = False
            break
        chain.append(parent)
        parent = nxt
    result = memo.get(parent, False)
    for p in reversed(chain):
        result = result or _is_reparse(p)
        memo[p] = result
    return result


def _protection_reason(path: str, excl_prefixes: tuple[str, ...], memo: dict[str, bool]) -> Optional[str]:
    if is_sensitive_norm(normalize_win_path(path)):
        return "Protected credential/key/wallet file"
    if is_under_excluded(path, excl_prefixes):
        return "Protected system/excluded path"
    if is_structural_folder_norm(normalize_win_path(path)):
        return "Protected system folder"
    if not os.path.lexists(path):
        return None
    if _is_reparse(path):
        return "Refused to delete symlink or junction"
    if _crosses_link(path, memo):
        return "Refused: path goes through a symlink or junction"
    return None


class _KeeperCheck:
    """Confirms a duplicate copy still has an identical, physically distinct twin left on disk."""

    def __init__(self, keepers: Mapping[str, Sequence[str]], memo: dict[str, bool]):
        self._keepers = keepers
        self._memo = memo
        self._hashes: dict[str, Optional[str]] = {}

    def _hash(self, path: str, size: int) -> Optional[str]:
        if path not in self._hashes:
            self._hashes[path] = compute_full_hash(path, size)
        return self._hashes[path]

    def problem(self, item: ScanItem) -> Optional[str]:
        kept = [k for k in self._keepers.get(item.group_id or "", ()) if k]
        if not kept:
            return None  # the user deliberately selected the whole group (warned in the UI)
        own_id = file_identity(item.path)
        own_hash = self._hash(item.path, item.size)
        if own_hash is None:
            return "Could not re-read file to verify it is still a duplicate"
        for k in kept:
            if not os.path.isfile(k) or _is_reparse(k) or _crosses_link(k, self._memo):
                continue
            k_id = file_identity(k)
            if own_id is not None and k_id == own_id:
                return "Kept copy is the same physical file (hardlink/alias)"
            if self._hash(k, item.size) == own_hash:
                return None
        return "No identical copy left to keep; rescan required"


def clean_items(
    items: Iterable[ScanItem],
    *,
    custom_exclusions: Sequence[str] = (),
    verify_identity: bool = True,
    keepers: Optional[Mapping[str, Sequence[str]]] = None,
) -> CleanupResult:
    """Safely move validated ScanItems to the Windows Recycle Bin.

    Re-verifies before each operation:
    - Path is not in excluded or critical system locations, nor a structural folder.
    - Neither the path nor any parent is a symlink or junction/reparse point.
    - File still exists and its size/mtime have not changed since scan.
    - Empty directory is still genuinely empty right before removal.
    - Duplicate copies (when `keepers` maps group_id -> paths being kept): one kept file is
      still present, byte-identical and not the same physical file.
    """
    excl_prefixes = protected_prefixes(custom_exclusions)
    memo: dict[str, bool] = {}
    keeper_check = _KeeperCheck(keepers, memo) if keepers is not None else None
    result = CleanupResult()
    touched_drives: set[str] = set()

    for item in items:
        path = item.path
        reason = _protection_reason(path, excl_prefixes, memo)
        if reason:
            result.failed.append((path, reason))
            continue

        if not os.path.exists(path):
            result.skipped_changed.append((path, "File no longer exists"))
            continue

        if verify_identity:
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                result.failed.append((path, str(exc)))
                continue

            if "empty_dir" in item.categories:
                if not verify_truly_empty_dir(path):
                    result.skipped_changed.append((path, "Directory is no longer empty"))
                    continue
            else:
                if st.st_size != item.size or abs(st.st_mtime - item.mtime) > 1.0:
                    result.skipped_changed.append(
                        (path, "File was modified after scan; rescan required")
                    )
                    continue

        if keeper_check and item.group_id and "duplicate" in item.categories:
            problem = keeper_check.problem(item)
            if problem:
                result.skipped_changed.append((path, problem))
                continue

        try:
            send2trash(os.path.normpath(path))
            result.trashed.append(path)
            result.bytes_sent += max(0, item.size)
            item.status = "trashed"
            item.selected = False
            if item.drive:
                touched_drives.add(item.drive)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to send %s to Recycle Bin: %s", basename_for_log(path), exc)
            result.failed.append((path, str(exc)))
            item.status = "error"

    for drv in touched_drives:
        invalidate_drive_cache(drv)

    return result


def send_to_trash(
    paths: Iterable[str],
    *,
    custom_exclusions: Sequence[str] = (),
) -> tuple[list[str], list[tuple[str, str]]]:
    """Move raw paths to the Recycle Bin after checking exclusion boundaries."""
    excl_prefixes = protected_prefixes(custom_exclusions)
    memo: dict[str, bool] = {}
    ok: list[str] = []
    failed: list[tuple[str, str]] = []
    for path in paths:
        reason = _protection_reason(path, excl_prefixes, memo)
        if reason:
            failed.append((path, reason))
            continue
        if not os.path.exists(path):
            failed.append((path, "File not found"))
            continue
        try:
            send2trash(os.path.normpath(path))
            ok.append(path)
        except Exception as exc:  # noqa: BLE001
            failed.append((path, str(exc)))
    return ok, failed
