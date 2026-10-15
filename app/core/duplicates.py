from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from app.core.disk import to_long_path
from app.core.models import FileRecord

from app.core.disk import recommended_workers

try:
    import xxhash

    def _new_fast_hasher() -> object:
        return xxhash.xxh3_64()

except ImportError:

    def _new_fast_hasher() -> object:
        return hashlib.blake2b(digest_size=16)


def _new_full_hasher() -> object:
    # Confirmation must be collision-resistant, not just fast: BLAKE2b-256 over every byte.
    return hashlib.blake2b(digest_size=32)


FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
# Reading these would download them from the cloud / tape: never hash them.
NOT_LOCAL_ATTRS = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

PARTIAL_CHUNK = 64 * 1024  # 64 KB head/tail sample
FULL_CHUNK = 1024 * 1024   # 1 MB streaming read
SMALL_FILE_LIMIT = PARTIAL_CHUNK * 3
FULL_MARK = ":F:"


def compute_partial_hash(path: str, expected_size: int) -> Optional[str]:
    """Pre-filter hash on head, middle and tail.

    Files up to SMALL_FILE_LIMIT are read entirely anyway, so they get the full
    BLAKE2b digest directly (key contains FULL_MARK) and skip the second pass.
    """
    if expected_size <= 0:
        return None
    long_p = to_long_path(path)
    try:
        st = os.stat(long_p, follow_symlinks=False)
        if st.st_size != expected_size:
            return None
        with open(long_p, "rb") as f:
            if expected_size <= SMALL_FILE_LIMIT:
                data = f.read(expected_size + 1)
                if len(data) != expected_size:
                    return None
                full = _new_full_hasher()
                full.update(data)  # type: ignore[attr-defined]
                return f"{expected_size}{FULL_MARK}{full.hexdigest()}"  # type: ignore[attr-defined]
            h = _new_fast_hasher()
            h.update(f.read(PARTIAL_CHUNK))  # type: ignore[attr-defined]
            f.seek(expected_size // 2 - PARTIAL_CHUNK // 2)
            h.update(f.read(PARTIAL_CHUNK))  # type: ignore[attr-defined]
            f.seek(expected_size - PARTIAL_CHUNK)
            h.update(f.read(PARTIAL_CHUNK))  # type: ignore[attr-defined]
        return f"{expected_size}:{h.hexdigest()}"  # type: ignore[attr-defined]
    except OSError:
        return None


def compute_full_hash(
    path: str,
    expected_size: int,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    """Compute a full-content cryptographic/128-bit hash reading 100% of the file."""
    if expected_size <= 0:
        return None
    long_p = to_long_path(path)
    try:
        h = _new_full_hasher()
        bytes_read = 0
        with open(long_p, "rb") as f:
            while True:
                if should_cancel and should_cancel():
                    return None
                chunk = f.read(FULL_CHUNK)
                if not chunk:
                    break
                bytes_read += len(chunk)
                h.update(chunk)  # type: ignore[attr-defined]
        if bytes_read != expected_size:
            return None
        return f"{expected_size}:{h.hexdigest()}"  # type: ignore[attr-defined]
    except OSError:
        return None


def file_identity(path: str) -> Optional[tuple[int, int]]:
    """(volume serial, file index): equal for hardlinks and for one file reached via two paths."""
    try:
        st = os.stat(to_long_path(path), follow_symlinks=False)
    except OSError:
        return None
    if not st.st_ino:
        return None
    return (st.st_dev, st.st_ino)


def _distinct_files(group: list[FileRecord]) -> list[FileRecord]:
    seen: set[tuple[int, int]] = set()
    out: list[FileRecord] = []
    for rec in group:
        ident = file_identity(rec.path)
        if ident is not None:
            if ident in seen:
                continue
            seen.add(ident)
        out.append(rec)
    return out


def find_duplicate_groups(
    records: list[FileRecord],
    *,
    min_size_bytes: int = 1024,
    max_workers: int = 0,
    should_cancel: Optional[Callable[[], bool]] = None,
    wait_if_paused: Optional[Callable[[], None]] = None,
    on_progress: Optional[Callable[[str, int], None]] = None,
) -> list[tuple[str, list[FileRecord]]]:
    """Progressive duplicate finder:
    1. Group by exact byte size (>= min_size_bytes, > 0).
    2. Prune size groups with < 2 files.
    3. Compute partial hash on size collisions.
    4. Prune partial-hash groups with < 2 files.
    5. Compute FULL content hash on remaining candidates to guarantee 100% byte-identical groups.
    6. Return [(group_id, [primary_record, dup_record_1, ...]), ...] sorted by total wasted size.
    """
    effective_min = max(1, min_size_bytes)
    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for rec in records:
        if rec.is_dir or rec.size < effective_min or rec.attributes & NOT_LOCAL_ATTRS:
            continue
        by_size[rec.size].append(rec)

    # Deduplicate identical paths if any
    size_candidates: list[FileRecord] = []
    for sz, group in by_size.items():
        if len(group) >= 2:
            seen_paths: set[str] = set()
            unique_group: list[FileRecord] = []
            for r in group:
                norm = os.path.normcase(r.path)
                if norm not in seen_paths:
                    seen_paths.add(norm)
                    unique_group.append(r)
            if len(unique_group) >= 2:
                size_candidates.extend(unique_group)

    if not size_candidates:
        return []

    workers = max_workers if max_workers > 0 else recommended_workers(size_candidates[0].drive or size_candidates[0].path)

    # Stage 1: Partial hash
    partial_groups: dict[str, list[FileRecord]] = defaultdict(list)
    processed = 0

    def _partial_task(rec: FileRecord) -> tuple[FileRecord, Optional[str]]:
        if wait_if_paused:
            wait_if_paused()
        if should_cancel and should_cancel():
            return (rec, None)
        return (rec, compute_partial_hash(rec.path, rec.size))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_partial_task, r) for r in size_candidates]
        for fut in as_completed(futs):
            if should_cancel and should_cancel():
                break
            rec, ph = fut.result()
            processed += 1
            if ph:
                partial_groups[ph].append(rec)
            if on_progress and processed % 64 == 0:
                on_progress(rec.path, processed)

    if should_cancel and should_cancel():
        return []

    # Stage 2: Full content hash verification (mandatory to prevent false positives)
    full_groups: dict[str, list[FileRecord]] = defaultdict(list)
    full_candidates: list[FileRecord] = []
    for ph, group in partial_groups.items():
        if len(group) < 2:
            continue
        if FULL_MARK in ph:
            full_groups[ph].extend(group)
        else:
            full_candidates.extend(group)

    def _full_task(rec: FileRecord) -> tuple[FileRecord, Optional[str]]:
        if wait_if_paused:
            wait_if_paused()
        if should_cancel and should_cancel():
            return (rec, None)
        return (rec, compute_full_hash(rec.path, rec.size, should_cancel=should_cancel))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_full_task, r) for r in full_candidates]
        for fut in as_completed(futs):
            if should_cancel and should_cancel():
                break
            rec, fh = fut.result()
            processed += 1
            if fh:
                full_groups[fh].append(rec)
            if on_progress and processed % 32 == 0:
                on_progress(rec.path, processed)

    if should_cancel and should_cancel():
        return []

    confirmed: list[tuple[int, list[FileRecord]]] = []
    for fh, group in full_groups.items():
        if len(group) < 2:
            continue
        # Deleting one alias of a single physical file would destroy the only copy.
        group = _distinct_files(group)
        if len(group) < 2:
            continue
        # Sort deterministically: newest mtime first, then shortest path as primary "keep" copy
        sorted_group = sorted(group, key=lambda r: (-r.mtime, len(r.path), r.path.lower()))
        wasted = sorted_group[0].size * (len(sorted_group) - 1)
        confirmed.append((wasted, sorted_group))

    confirmed.sort(key=lambda item: item[0], reverse=True)
    return [(f"dup-{idx + 1}", grp) for idx, (_, grp) in enumerate(confirmed)]
