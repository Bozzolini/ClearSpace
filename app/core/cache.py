from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from app.core.exclusions import normalize_win_path
from app.core.logging_config import get_app_data_dir, get_logger
from app.core.models import FileRecord, ScanHistoryEntry, ScanItem

log = get_logger("clearspace.cache")

_CUSTOM_DB_PATH: Optional[Path] = None

# New files cannot be detected without walking the disk, so cached data expires.
CACHE_MAX_AGE_SEC = 6 * 3600.0
INDEX_FULL_VALIDATION_LIMIT = 2000
INDEX_SAMPLE_SIZE = 512


def set_cache_db_path(path: Optional[Path]) -> None:
    """Override cache DB path (used by isolated unit tests)."""
    global _CUSTOM_DB_PATH
    _CUSTOM_DB_PATH = path


def _cache_dir() -> Path:
    base = get_app_data_dir() / "cache"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _db_path() -> Path:
    if _CUSTOM_DB_PATH is not None:
        _CUSTOM_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return _CUSTOM_DB_PATH
    return _cache_dir() / "scans.db"


_PURGED_DBS: set[str] = set()


def _connect() -> sqlite3.Connection:
    db = str(_db_path())
    conn = sqlite3.connect(db, timeout=30)
    # Deleted rows (file paths) are zeroed on disk instead of lingering in free pages.
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS drive_index_meta (
            drive TEXT PRIMARY KEY,
            indexed_at REAL NOT NULL,
            files_seen INTEGER NOT NULL,
            bytes_indexed INTEGER NOT NULL,
            exclusions_key TEXT NOT NULL,
            is_complete INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS drive_file_index (
            drive TEXT NOT NULL,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            is_dir INTEGER NOT NULL,
            is_empty_dir INTEGER NOT NULL,
            attributes INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_drive_file_index_drive
            ON drive_file_index(drive);

        CREATE TABLE IF NOT EXISTS drive_results (
            drive TEXT NOT NULL,
            modes_key TEXT NOT NULL,
            options_key TEXT NOT NULL,
            scanned_at REAL NOT NULL,
            files_seen INTEGER NOT NULL,
            is_complete INTEGER NOT NULL DEFAULT 1,
            payload TEXT NOT NULL,
            PRIMARY KEY (drive, modes_key, options_key)
        );

        CREATE TABLE IF NOT EXISTS scan_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            drives TEXT NOT NULL,
            modes TEXT NOT NULL,
            files_seen INTEGER NOT NULL,
            items_found INTEGER NOT NULL,
            potential_bytes INTEGER NOT NULL,
            duration_sec REAL NOT NULL,
            status TEXT NOT NULL
        );
        """
    )
    # Ensure backwards compatibility if old drive_results lacked is_complete
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(drive_results)").fetchall()]
        if "is_complete" not in cols:
            conn.execute("ALTER TABLE drive_results ADD COLUMN is_complete INTEGER NOT NULL DEFAULT 1")
            conn.commit()
    except sqlite3.Error:
        pass
    if db not in _PURGED_DBS:
        _PURGED_DBS.add(db)
        _purge_expired(conn)
        if _CUSTOM_DB_PATH is None:
            from app.core.privacy import restrict_to_current_user

            restrict_to_current_user(_cache_dir())
            restrict_to_current_user(db)
    return conn


def _purge_expired(conn: sqlite3.Connection, max_age_sec: float = CACHE_MAX_AGE_SEC) -> None:
    """Expired indexes are never used again, so they are not kept around either."""
    cutoff = time.time() - max_age_sec
    try:
        with conn:
            stale = [r[0] for r in conn.execute(
                "SELECT drive FROM drive_index_meta WHERE indexed_at < ?", (cutoff,)
            )]
            for drive in stale:
                conn.execute("DELETE FROM drive_file_index WHERE drive = ?", (drive,))
                conn.execute("DELETE FROM drive_index_meta WHERE drive = ?", (drive,))
            conn.execute("DELETE FROM drive_results WHERE scanned_at < ?", (cutoff,))
            conn.execute(
                "DELETE FROM drive_file_index WHERE drive NOT IN (SELECT drive FROM drive_index_meta)"
            )
    except sqlite3.Error as exc:
        log.warning("Could not purge expired cache: %s", exc)


def _compact(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")


def wipe_all_data() -> None:
    """Delete every file path ClearSpace ever stored: index, results and history."""
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM drive_results")
            conn.execute("DELETE FROM drive_file_index")
            conn.execute("DELETE FROM drive_index_meta")
            conn.execute("DELETE FROM scan_history")
        _compact(conn)
    finally:
        conn.close()
    # Leftover WAL pages can still hold paths until the files are removed.
    if _CUSTOM_DB_PATH is None:
        db = _db_path()
        for extra in (Path(str(db) + "-wal"), Path(str(db) + "-shm")):
            try:
                extra.unlink(missing_ok=True)
            except OSError:
                pass


def _norm_drive(drive: str) -> str:
    return normalize_win_path(drive)


def _modes_key(modes: Sequence[str]) -> str:
    return ",".join(sorted(set(modes)))


def _options_key(options: dict[str, Any]) -> str:
    keys = (
        "min_mb",
        "min_kb",
        "days",
        "days_old",
        "days_downloads",
        "custom_exclusions",
        "custom_temp_dirs",
        "custom_download_dirs",
    )
    slim: dict[str, Any] = {}
    for k in keys:
        if k in options and options[k] is not None:
            val = options[k]
            if isinstance(val, (list, tuple)):
                slim[k] = [normalize_win_path(str(x)) for x in val if x]
            elif isinstance(val, (int, float)):
                slim[k] = float(val)
            else:
                slim[k] = val
    return json.dumps(slim, sort_keys=True)


def _exclusions_key(custom_exclusions: Sequence[str] = ()) -> str:
    return json.dumps(sorted(normalize_win_path(p) for p in custom_exclusions if p))


def save_drive_index(
    drive: str,
    records: list[FileRecord],
    *,
    custom_exclusions: Sequence[str] = (),
    is_complete: bool = True,
) -> None:
    """Persist raw file metadata index for a drive (only when scan is complete)."""
    if not is_complete:
        return
    dkey = _norm_drive(drive)
    files_seen = sum(1 for r in records if not r.is_dir)
    bytes_indexed = sum(r.size for r in records if not r.is_dir)
    excl_key = _exclusions_key(custom_exclusions)

    rows = [
        (
            dkey,
            r.path,
            int(r.size),
            float(r.mtime),
            1 if r.is_dir else 0,
            1 if r.is_empty_dir else 0,
            int(r.attributes),
        )
        for r in records
    ]

    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM drive_file_index WHERE drive = ?", (dkey,))
            conn.executemany(
                """
                INSERT INTO drive_file_index
                (drive, path, size, mtime, is_dir, is_empty_dir, attributes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO drive_index_meta
                (drive, indexed_at, files_seen, bytes_indexed, exclusions_key, is_complete)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (dkey, time.time(), files_seen, bytes_indexed, excl_key),
            )
    finally:
        conn.close()


def load_drive_index(
    drive: str,
    *,
    custom_exclusions: Sequence[str] = (),
    max_age_sec: float = CACHE_MAX_AGE_SEC,
    validate_entries: bool = True,
) -> Optional[tuple[list[FileRecord], float]]:
    """Load cached FileRecord index for `drive` if complete, fresh, and valid."""
    dkey = _norm_drive(drive)
    excl_key = _exclusions_key(custom_exclusions)
    conn = _connect()
    try:
        meta = conn.execute(
            """
            SELECT indexed_at, exclusions_key, is_complete
            FROM drive_index_meta WHERE drive = ?
            """,
            (dkey,),
        ).fetchone()
        if not meta:
            return None
        indexed_at, saved_excl_key, is_complete = meta
        if not is_complete or saved_excl_key != excl_key:
            return None
        if (time.time() - float(indexed_at)) > max_age_sec:
            return None

        rows = conn.execute(
            """
            SELECT path, size, mtime, is_dir, is_empty_dir, attributes
            FROM drive_file_index WHERE drive = ?
            """,
            (dkey,),
        ).fetchall()
    finally:
        conn.close()

    if validate_entries:
        # Small indexes are fully verified; large ones are spot-checked with a random sample.
        # Items produced from the index are re-verified individually afterwards (validate_item).
        sample = rows if len(rows) <= INDEX_FULL_VALIDATION_LIMIT else random.sample(rows, INDEX_SAMPLE_SIZE)
        for path, size, mtime, is_dir, _empty, _attrs in sample:
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError:
                return None
            if not is_dir and (st.st_size != int(size) or abs(st.st_mtime - float(mtime)) > 1e-3):
                return None

    records: list[FileRecord] = []
    for path, size, mtime, is_dir, is_empty_dir, attrs in rows:
        records.append(
            FileRecord(
                path=path,
                size=int(size),
                mtime=float(mtime),
                is_dir=bool(is_dir),
                drive=drive,
                is_empty_dir=bool(is_empty_dir),
                attributes=int(attrs),
            )
        )
    return records, float(indexed_at)


def save_drive_result(
    drive: str,
    modes: list[str],
    options: dict[str, Any],
    items: list[ScanItem],
    files_seen: int,
    *,
    is_complete: bool = True,
) -> None:
    """Save filtered scan results for a drive. Never persists partial/cancelled scans."""
    if not is_complete:
        return
    dkey = _norm_drive(drive)
    payload = [
        {
            "path": i.path,
            "size": i.size,
            "mtime": i.mtime,
            "reason": i.reason,
            "categories": list(i.categories),
            "drive": i.drive or drive,
            "group_id": i.group_id,
            "is_primary_in_group": i.is_primary_in_group,
            "safety": i.safety,
        }
        for i in items
    ]
    conn = _connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO drive_results
                (drive, modes_key, options_key, scanned_at, files_seen, is_complete, payload)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    dkey,
                    _modes_key(modes),
                    _options_key(options),
                    time.time(),
                    int(files_seen),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
    finally:
        conn.close()


def validate_item(item: ScanItem) -> bool:
    """Verify a cached ScanItem still exists and matches its cached size/mtime."""
    try:
        st = os.stat(item.path, follow_symlinks=False)
    except OSError:
        return False
    if "empty_dir" in item.categories:
        try:
            with os.scandir(item.path) as it:
                return next(it, None) is None
        except OSError:
            return False
    return st.st_size == item.size and abs(st.st_mtime - item.mtime) <= 1.0


def load_drive_result(
    drive: str,
    modes: list[str],
    options: dict[str, Any],
    *,
    verify_on_disk: bool = True,
    max_age_sec: float = CACHE_MAX_AGE_SEC,
) -> Optional[tuple[list[ScanItem], int, float]]:
    dkey = _norm_drive(drive)
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT scanned_at, files_seen, is_complete, payload FROM drive_results
            WHERE drive = ? AND modes_key = ? AND options_key = ?
            """,
            (dkey, _modes_key(modes), _options_key(options)),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    scanned_at, files_seen, is_complete, payload = row
    if not is_complete or (time.time() - float(scanned_at)) > max_age_sec:
        return None
    data = json.loads(payload)
    items: list[ScanItem] = []
    for d in data:
        cats = tuple(d.get("categories") or ([d["reason"]] if d.get("reason") else []))
        item = ScanItem(
            path=d["path"],
            size=int(d["size"]),
            mtime=float(d["mtime"]),
            reason=d["reason"],
            categories=cats,
            drive=d.get("drive", drive),
            group_id=d.get("group_id"),
            is_primary_in_group=bool(d.get("is_primary_in_group", False)),
            safety=d.get("safety", "candidate"),
        )
        if verify_on_disk and not validate_item(item):
            # If any cached item was deleted or modified on disk, invalidate stale result
            invalidate_drive_cache(drive)
            return None
        items.append(item)
    return items, int(files_seen), float(scanned_at)


def has_drive_cache(drive: str, modes: list[str], options: dict[str, Any]) -> bool:
    return load_drive_result(drive, modes, options, verify_on_disk=True) is not None


def list_cached_drives(modes: list[str], options: dict[str, Any]) -> list[tuple[str, float, int]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT drive, scanned_at, files_seen FROM drive_results
            WHERE modes_key = ? AND options_key = ? AND is_complete = 1
            ORDER BY drive
            """,
            (_modes_key(modes), _options_key(options)),
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], float(r[1]), int(r[2])) for r in rows]


def invalidate_drive_cache(drive: str) -> None:
    dkey = _norm_drive(drive)
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM drive_results WHERE drive = ?", (dkey,))
            conn.execute("DELETE FROM drive_file_index WHERE drive = ?", (dkey,))
            conn.execute("DELETE FROM drive_index_meta WHERE drive = ?", (dkey,))
    finally:
        conn.close()


def clear_drive_cache(drive: Optional[str] = None) -> None:
    if drive:
        invalidate_drive_cache(drive)
        return
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM drive_results")
            conn.execute("DELETE FROM drive_file_index")
            conn.execute("DELETE FROM drive_index_meta")
        _compact(conn)
    finally:
        conn.close()


def record_scan_history(
    drives: Sequence[str],
    modes: Sequence[str],
    files_seen: int,
    items_found: int,
    potential_bytes: int,
    duration_sec: float,
    status: str,
) -> None:
    conn = _connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO scan_history
                (timestamp, drives, modes, files_seen, items_found, potential_bytes, duration_sec, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    json.dumps(list(drives)),
                    json.dumps(list(modes)),
                    int(files_seen),
                    int(items_found),
                    int(potential_bytes),
                    float(duration_sec),
                    status,
                ),
            )
    except sqlite3.Error as exc:
        log.warning("Could not record scan history: %s", exc)
    finally:
        conn.close()


def list_scan_history(limit: int = 50) -> list[ScanHistoryEntry]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, timestamp, drives, modes, files_seen, items_found, potential_bytes, duration_sec, status
            FROM scan_history ORDER BY id DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    entries: list[ScanHistoryEntry] = []
    for r in rows:
        entries.append(
            ScanHistoryEntry(
                id=int(r[0]),
                timestamp=float(r[1]),
                drives=json.loads(r[2]),
                modes=json.loads(r[3]),
                files_seen=int(r[4]),
                items_found=int(r[5]),
                potential_bytes=int(r[6]),
                duration_sec=float(r[7]),
                status=str(r[8]),
            )
        )
    return entries
