"""Pipeline guarantees: only files that match a selected mode ever reach the results."""
from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path

import pytest

from app.core.classifier import ALL_MODES, MODE_TO_CATEGORY, classify_and_filter, is_recognized_temp_file
from app.core.engine import scan_drive
from app.core.models import FileRecord, SafetyLevel, ScanItem, ScanParameters

from conftest import DAY, write

MB = 1024 * 1024


def scan(root: Path, modes: list[str], **opts: object) -> list[ScanItem]:
    opts.setdefault("min_kb", 1)
    res = scan_drive(str(root), modes, dict(opts), use_cache=False, force=True)
    assert res.complete
    return res.items


def names(items: list[ScanItem]) -> set[str]:
    return {i.name for i in items}


def assert_only_selected(items: list[ScanItem], modes: list[str]) -> None:
    allowed = {MODE_TO_CATEGORY[m] for m in modes}
    for it in items:
        assert it.categories, it
        assert set(it.categories) <= allowed, it
    paths = [os.path.normcase(i.path) for i in items]
    assert len(paths) == len(set(paths)), "a file must appear once, with all its categories"


@pytest.fixture
def mixed_tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    write(root / "big.iso", 3 * MB)
    write(root / "medium.zip", MB // 2)
    write(root / "notes.txt", 200)
    write(root / "old_report.docx", 300, age_days=500)
    write(root / "Downloads" / "old_setup.exe", 400, age_days=200)
    write(root / "Downloads" / "new_setup.exe", 400, age_days=5)
    write(root / "deep" / "project" / "downloads" / "asset.bin", 400, age_days=200)
    write(root / "work" / "~$draft.docx", 100)
    write(root / "work" / "session.tmp", 100)
    write(root / "work" / "real.docx", 100)
    write(root / "app" / "GPUCache" / "data_1", 5000, fill=b"gpu")
    write(root / "app" / "Cache" / "f_000123", 5000, fill=b"blob")
    write(root / "app" / "settings.json", 5000, fill=b"{}")
    write(root / "photos" / "a.jpg", 70_000, fill=b"PHOTO")
    write(root / "photos" / "copy_of_a.jpg", 70_000, fill=b"PHOTO")
    write(root / "photos" / "b.jpg", 70_000, fill=b"OTHER")
    (root / "empty_dir").mkdir()
    write(root / "has_file" / "x.txt", 5)
    return root


def test_large_files_only(mixed_tree: Path) -> None:
    items = scan(mixed_tree, ["large_files"], min_mb=1)
    assert names(items) == {"big.iso"}
    assert all(i.size >= MB for i in items)
    assert_only_selected(items, ["large_files"])


def test_large_files_threshold_boundary(tmp_path: Path) -> None:
    write(tmp_path / "r" / "exact.bin", 2 * MB)
    write(tmp_path / "r" / "below.bin", 2 * MB - 1)
    (tmp_path / "r" / "bigdir").mkdir()
    items = scan(tmp_path / "r", ["large_files"], min_mb=2)
    assert names(items) == {"exact.bin"}


def test_duplicates_only_identical_groups(mixed_tree: Path) -> None:
    items = scan(mixed_tree, ["duplicates"])
    assert names(items) == {"a.jpg", "copy_of_a.jpg"}
    assert len({i.group_id for i in items}) == 1
    primaries = [i for i in items if i.is_primary_in_group]
    assert len(primaries) == 1
    assert primaries[0].safety == SafetyLevel.CANDIDATE.value
    assert not any(i.selected for i in items)
    assert_only_selected(items, ["duplicates"])


def test_false_duplicate_same_size_head_mid_tail(tmp_path: Path) -> None:
    """Same size and identical sampled regions, one differing byte elsewhere: not a duplicate."""
    root = tmp_path / "r"
    root.mkdir()
    data = bytearray(os.urandom(MB))
    (root / "a.bin").write_bytes(bytes(data))
    data[300_000] ^= 0xFF  # outside the 64 KB head / middle / tail samples
    (root / "b.bin").write_bytes(bytes(data))
    (root / "c.bin").write_bytes((root / "a.bin").read_bytes())

    from app.core.duplicates import compute_full_hash, compute_partial_hash

    assert compute_partial_hash(str(root / "a.bin"), MB) == compute_partial_hash(str(root / "b.bin"), MB)
    assert compute_full_hash(str(root / "a.bin"), MB) != compute_full_hash(str(root / "b.bin"), MB)

    items = scan(root, ["duplicates"])
    assert names(items) == {"a.bin", "c.bin"}


def test_small_false_duplicates_rejected(tmp_path: Path) -> None:
    root = tmp_path / "r"
    write(root / "x.dat", 5000, fill=b"A")
    write(root / "y.dat", 5000, fill=b"B")
    assert scan(root, ["duplicates"]) == []


def test_temp_only_recognized_files(mixed_tree: Path) -> None:
    items = scan(mixed_tree, ["temp"])
    assert names(items) == {"~$draft.docx", "session.tmp", "data_1", "f_000123"}
    assert "real.docx" not in names(items)
    assert "settings.json" not in names(items)
    assert all(not os.path.isdir(i.path) for i in items)
    assert_only_selected(items, ["temp"])


def test_temp_rules_do_not_flag_normal_files_in_temp_or_localappdata() -> None:
    local = os.environ.get("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    temp = os.environ.get("TEMP", r"C:\Users\u\AppData\Local\Temp")
    assert not is_recognized_temp_file(os.path.join(local, "Vendor", "App", "config.xml"))
    assert not is_recognized_temp_file(os.path.join(temp, "project", "main.py"))
    assert not is_recognized_temp_file(os.path.join(temp, "backup.bak"))
    assert is_recognized_temp_file(os.path.join(temp, "x", "install.log"))
    assert is_recognized_temp_file(os.path.join(local, "Google", "Chrome", "GPUCache", "data_0"))


def test_old_files_use_modification_time(tmp_path: Path) -> None:
    root = tmp_path / "r"
    old = write(root / "old_mtime.txt", 10)
    recent = write(root / "old_access_only.txt", 10)
    now = time.time()
    os.utime(old, (now, now - 400 * DAY))          # atime recent, mtime old
    os.utime(recent, (now - 400 * DAY, now))       # atime old, mtime recent
    write(root / "fresh.txt", 10)
    items = scan(root, ["old_files"], days_old=365)
    assert names(items) == {"old_mtime.txt"}
    assert all(i.mtime < now - 365 * DAY for i in items)


def test_empty_dirs_only_truly_empty(mixed_tree: Path) -> None:
    hidden_dir = mixed_tree / "only_hidden"
    hidden_dir.mkdir()
    hidden = write(hidden_dir / "secret.ini", 3)
    ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2 | 0x4)  # hidden + system
    items = scan(mixed_tree, ["empty_dirs"])
    assert names(items) == {"empty_dir"}
    assert_only_selected(items, ["empty_dirs"])


def test_scan_root_never_reported_as_empty(tmp_path: Path) -> None:
    root = tmp_path / "empty_root"
    root.mkdir()
    assert scan(root, ["empty_dirs"]) == []


def test_old_downloads_only_inside_downloads(mixed_tree: Path) -> None:
    items = scan(
        mixed_tree,
        ["old_downloads"],
        days_downloads=90,
        custom_download_dirs=[str(mixed_tree / "Downloads")],
    )
    assert names(items) == {"old_setup.exe"}
    assert_only_selected(items, ["old_downloads"])


def test_all_modes_combined(mixed_tree: Path) -> None:
    write(mixed_tree / "Downloads" / "huge_old.iso", 2 * MB, age_days=400)
    items = scan(
        mixed_tree,
        ALL_MODES,
        min_mb=1,
        days_old=365,
        days_downloads=90,
        custom_download_dirs=[str(mixed_tree / "Downloads")],
    )
    assert_only_selected(items, ALL_MODES)
    by = {i.name: i for i in items}
    assert set(by["huge_old.iso"].categories) == {"large_file", "old_file", "old_download"}
    assert by["big.iso"].categories == ("large_file",)
    for unrelated in ("notes.txt", "medium.zip", "real.docx", "settings.json", "b.jpg",
                      "new_setup.exe", "x.txt", "has_file", "asset.bin"):
        assert unrelated not in by, unrelated


@pytest.mark.parametrize("mode", ALL_MODES)
def test_each_mode_returns_nothing_unrelated(mixed_tree: Path, mode: str) -> None:
    items = scan(
        mixed_tree, [mode], min_mb=1, days_old=365, days_downloads=90,
        custom_download_dirs=[str(mixed_tree / "Downloads")],
    )
    assert_only_selected(items, [mode])
    assert "notes.txt" not in names(items)


def test_no_modes_no_results(mixed_tree: Path) -> None:
    assert scan(mixed_tree, []) == []


def test_classifier_drops_records_matching_no_mode() -> None:
    now = time.time()
    records = [
        FileRecord(path=rf"Z:\data\f{i}.txt", size=100, mtime=now, is_dir=False, drive="Z:\\")
        for i in range(1000)
    ]
    params = ScanParameters.from_options(["Z:\\"], ALL_MODES, {"min_mb": 1, "days_old": 365})
    assert classify_and_filter(records, params, now=now) == []


def test_large_record_count_classification_is_fast() -> None:
    now = time.time()
    records = [
        FileRecord(
            path=rf"Z:\bulk\d{i % 500}\file_{i}.dat",
            size=(i % 1000) * 4096,
            mtime=now - (i % 800) * DAY,
            is_dir=False,
            drive="Z:\\",
        )
        for i in range(200_000)
    ]
    params = ScanParameters.from_options(["Z:\\"], ["large_files", "old_files", "temp"], {"min_mb": 3, "days_old": 700})
    t0 = time.perf_counter()
    items = classify_and_filter(records, params, now=now)
    elapsed = time.perf_counter() - t0
    assert elapsed < 10.0
    assert all(i.size >= 3 * MB or i.mtime < now - 700 * DAY for i in items)
    assert len(items) == sum(1 for r in records if r.size >= 3 * MB or r.mtime < now - 700 * DAY)
