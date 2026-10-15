"""Exclusions, reparse points, permissions, Unicode/long paths and safe cleanup."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.core import fast_walk
from app.core.classifier import verify_truly_empty_dir
from app.core.disk import to_long_path
from app.core.engine import scan_drive
from app.core.exclusions import build_exclusion_prefixes, is_under_excluded, protected_prefixes
from app.core.models import ScanItem
from app.core.trash import clean_items

from conftest import write

MB = 1024 * 1024


def scan(root: Path, modes: list[str], **opts: object) -> list[ScanItem]:
    opts.setdefault("min_kb", 1)
    res = scan_drive(str(root), modes, dict(opts), use_cache=False, force=True)
    return res.items


def make_junction(link: Path, target: Path) -> None:
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        pytest.skip(f"cannot create junction: {proc.stderr}")


# ----------------------------------------------------------------- exclusions


def test_custom_exclusion_hides_subtree(tmp_path: Path) -> None:
    root = tmp_path / "r"
    write(root / "keep" / "big.bin", 2 * MB)
    write(root / "skip" / "big.bin", 2 * MB)
    items = scan(root, ["large_files"], min_mb=1, custom_exclusions=[str(root / "skip")])
    assert [Path(i.path).parent.name for i in items] == ["keep"]


def test_exclusion_respects_directory_boundaries(tmp_path: Path) -> None:
    prefixes = build_exclusion_prefixes([str(tmp_path / "foo")])
    assert is_under_excluded(str(tmp_path / "foo"), prefixes)
    assert is_under_excluded(str(tmp_path / "FOO" / "x.txt"), prefixes)
    assert not is_under_excluded(str(tmp_path / "foobar" / "x.txt"), prefixes)
    assert is_under_excluded("\\\\?\\" + str(tmp_path / "foo" / "y"), prefixes)


def test_custom_exclusions_never_drop_system_protection(tmp_path: Path) -> None:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    prefixes = protected_prefixes([str(tmp_path / "mine")])
    assert is_under_excluded(os.path.join(windir, "System32", "kernel32.dll"), prefixes)
    assert is_under_excluded(r"D:\$Recycle.Bin\S-1-5\file.txt", prefixes)
    assert is_under_excluded(r"E:\System Volume Information\x", prefixes)
    assert is_under_excluded(str(tmp_path / "mine" / "a.txt"), prefixes)


def test_cleanup_refuses_protected_paths(fake_trash: list[str], tmp_path: Path) -> None:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    notepad = os.path.join(windir, "notepad.exe")
    item = ScanItem(path=notepad, size=1, mtime=0, reason="large_file")
    res = clean_items([item], custom_exclusions=[str(tmp_path / "unrelated")])
    assert fake_trash == []
    assert res.trashed == []
    assert res.failed and "Protected" in res.failed[0][1]


# ----------------------------------------------------------------- reparse points


def test_junction_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside / "huge.bin", 2 * MB)
    root = tmp_path / "root"
    write(root / "inside.txt", 10)
    make_junction(root / "link_to_outside", outside)
    items = scan(root, ["large_files", "empty_dirs"], min_mb=1)
    assert items == []


def test_junction_into_excluded_area_does_not_bypass_exclusion(tmp_path: Path) -> None:
    secret = tmp_path / "excluded"
    write(secret / "big.bin", 2 * MB)
    root = tmp_path / "root"
    root.mkdir()
    make_junction(root / "j", secret)
    items = scan(root, ["large_files"], min_mb=1, custom_exclusions=[str(secret)])
    assert items == []


def test_symlink_is_not_followed(tmp_path: Path) -> None:
    target = write(tmp_path / "target" / "big.bin", 2 * MB)
    root = tmp_path / "root"
    root.mkdir()
    try:
        os.symlink(target, root / "link.bin")
        os.symlink(target.parent, root / "dirlink", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted")
    assert scan(root, ["large_files", "duplicates"], min_mb=1) == []


def test_cleanup_refuses_junction(fake_trash: list[str], tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.mkdir()
    link = tmp_path / "j"
    make_junction(link, target)
    res = clean_items([ScanItem(path=str(link), size=0, mtime=0, reason="empty_dir")])
    assert fake_trash == []
    assert res.failed and "junction" in res.failed[0][1]


# ----------------------------------------------------------------- permissions


def test_permission_error_directory_is_skipped_not_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    locked = root / "a" / "locked"
    locked.mkdir(parents=True)
    write(root / "a" / "ok" / "big.bin", 2 * MB)
    real_scandir = os.scandir
    locked_long = os.path.normcase(to_long_path(str(locked)))

    def guarded(path: str = ".") -> object:
        p = os.path.normcase(str(path))
        if p in (locked_long, os.path.normcase(str(locked))):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(fast_walk.os, "scandir", guarded)
    monkeypatch.setattr("app.core.classifier.os.scandir", guarded)
    items = scan(root, ["large_files", "empty_dirs"], min_mb=1)
    assert [i.name for i in items] == ["big.bin"]
    assert not verify_truly_empty_dir(str(locked))


def test_unreadable_file_is_not_a_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    write(root / "a.bin", 5000, fill=b"Z")
    write(root / "b.bin", 5000, fill=b"Z")
    write(root / "c.bin", 5000, fill=b"Z")
    import builtins

    real_open = builtins.open

    def guarded_open(file: object, *args: object, **kwargs: object) -> object:
        if str(file).endswith("c.bin"):
            raise PermissionError(13, "in use", str(file))
        return real_open(file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("app.core.duplicates.open", guarded_open, raising=False)
    items = scan(root, ["duplicates"])
    assert sorted(i.name for i in items) == ["a.bin", "b.bin"]


# ----------------------------------------------------------------- unicode & long paths


def test_unicode_paths(tmp_path: Path) -> None:
    root = tmp_path / "Ünïcødé_файлы_日本語"
    write(root / "vidéo_😀.mkv", 2 * MB, fill=b"U")
    write(root / "копия_😀.mkv", 2 * MB, fill=b"U")
    items = scan(root, ["large_files", "duplicates"], min_mb=1)
    assert {i.name for i in items} == {"vidéo_😀.mkv", "копия_😀.mkv"}
    assert all(set(i.categories) == {"large_file", "duplicate"} for i in items)


def test_long_paths_over_260_chars(tmp_path: Path) -> None:
    root = tmp_path / "r"
    deep = root
    while len(str(deep)) < 300:
        deep = deep / ("segment_" + "x" * 30)
    os.makedirs(to_long_path(str(deep)), exist_ok=True)
    target = deep / "deep_big.bin"
    with open(to_long_path(str(target)), "wb") as f:
        f.write(b"\0" * (2 * MB))
    items = scan(root, ["large_files"], min_mb=1)
    assert [i.name for i in items] == ["deep_big.bin"]
    assert len(items[0].path) > 260


# ----------------------------------------------------------------- cleanup identity checks


def test_cleanup_sends_unchanged_files(fake_trash: list[str], tmp_path: Path) -> None:
    root = tmp_path / "r"
    write(root / "a.tmp", 100)
    (root / "empty").mkdir()
    items = scan(root, ["temp", "empty_dirs"])
    res = clean_items(items)
    assert sorted(Path(p).name for p in res.trashed) == ["a.tmp", "empty"]
    assert res.bytes_sent == 100
    assert not (root / "a.tmp").exists()


def test_cleanup_skips_modified_deleted_or_refilled(fake_trash: list[str], tmp_path: Path) -> None:
    root = tmp_path / "r"
    write(root / "modified.tmp", 100)
    write(root / "deleted.tmp", 100)
    write(root / "same.tmp", 100)
    (root / "was_empty").mkdir()
    items = scan(root, ["temp", "empty_dirs"])
    assert len(items) == 4

    (root / "modified.tmp").write_bytes(b"changed content, different size")
    (root / "deleted.tmp").unlink()
    write(root / "was_empty" / "new_file.txt", 5)

    res = clean_items(items)
    assert [Path(p).name for p in res.trashed] == ["same.tmp"]
    skipped = sorted(Path(p).name for p, _ in res.skipped_changed)
    assert skipped == ["deleted.tmp", "modified.tmp", "was_empty"]
    assert (root / "modified.tmp").exists()
    assert (root / "was_empty" / "new_file.txt").exists()


def test_cleanup_handles_file_in_use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = write(tmp_path / "r" / "locked.tmp", 10)
    items = scan(tmp_path / "r", ["temp"])

    def failing(_p: str) -> None:
        raise OSError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr("app.core.trash.send2trash", failing)
    res = clean_items(items)
    assert res.trashed == []
    assert res.failed and "another process" in res.failed[0][1]
    assert f.exists()


def test_scan_root_that_is_a_junction_is_skipped(tmp_path: Path) -> None:
    real = tmp_path / "real"
    write(real / "huge.bin", 2 * MB)
    link = tmp_path / "link"
    make_junction(link, real)
    assert scan(link, ["large_files"], min_mb=1) == []


def test_sensitive_keys_and_wallets_are_never_scanned(tmp_path: Path) -> None:
    root = tmp_path / "r"
    write(root / "id_rsa", 2 * MB, fill=b"K")
    write(root / "cert.pem", 2 * MB, fill=b"P")
    write(root / "passwords.kdbx", 2 * MB, fill=b"B")
    write(root / "wallet.dat", 2 * MB, fill=b"W")
    write(root / "Chrome" / "Login Data", 2 * MB, fill=b"L")
    write(root / ".ssh" / "old_key", 2 * MB, fill=b"S")
    write(root / "ok.bin", 2 * MB, fill=b"O")
    items = scan(root, ["large_files", "old_files", "duplicates"], min_mb=1, days_old=0)
    assert [i.name for i in items] == ["ok.bin"]


def test_cleanup_refuses_private_key_even_if_forced(fake_trash: list[str], tmp_path: Path) -> None:
    key = write(tmp_path / "id_rsa", 100)
    res = clean_items([ScanItem(path=str(key), size=100, mtime=0, reason="old_file", categories=("old_file",))])
    assert fake_trash == []
    assert key.exists()
    assert res.failed and "credential" in res.failed[0][1].lower()


def test_keepers_block_deleting_last_duplicate(fake_trash: list[str], tmp_path: Path) -> None:
    root = tmp_path / "r"
    write(root / "a.bin", 5000, fill=b"Z")
    write(root / "b.bin", 5000, fill=b"Z")
    items = scan(root, ["duplicates"])
    by_name = {i.name: i for i in items}
    copy = by_name["b.bin"]
    primary = by_name["a.bin"]
    if copy.is_primary_in_group:
        copy, primary = primary, copy
    os.remove(primary.path)
    res = clean_items([copy], keepers={copy.group_id or "": (primary.path,)})
    assert fake_trash == []
    assert copy.path not in res.trashed
    assert any("keep" in msg.lower() or "identical" in msg.lower() for _, msg in res.skipped_changed)


def test_real_recycle_bin_on_temp_file(tmp_path: Path) -> None:
    """One end-to-end call to the real Windows Recycle Bin, on a throwaway temp file only."""
    f = write(tmp_path / "r" / "clearspace_selftest.tmp", 16)
    items = scan(tmp_path / "r", ["temp"])
    res = clean_items(items)
    assert res.trashed == [str(f)]
    assert not f.exists()
