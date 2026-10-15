"""The real window stays responsive during a scan and shows only valid results."""
from __future__ import annotations

import os
import time
import tkinter as tk
from pathlib import Path

import pytest

from app.core.disk import format_size
from app.core.models import SafetyLevel, ScanState

from conftest import write

MB = 1024 * 1024


@pytest.fixture(scope="module")
def _window(tmp_path_factory: pytest.TempPathFactory):
    # One Tk interpreter per process: creating several in a row intermittently fails on Windows Tcl.
    if os.environ.get("CLEARSPACE_SKIP_UI"):
        pytest.skip("UI tests disabled")
    appdata = tmp_path_factory.mktemp("appdata")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.core.config._config_path", lambda: appdata / "config.json")
        try:
            from app.ui.main_window import MainWindow

            window = MainWindow()
        except tk.TclError as exc:
            pytest.skip(f"no display: {exc}")
        window.update()
        yield window
        window._on_close()


@pytest.fixture
def app(_window):
    _window.items = []
    _window._items_by_path = {}
    _window.clear_selection()
    for table in _window.tables:
        table.search_var.set("")
    _window._refresh_tables()
    yield _window


def run_until_done(app, timeout: float = 60.0) -> list[float]:
    gaps: list[float] = []
    last = [time.perf_counter()]

    def tick() -> None:
        now = time.perf_counter()
        gaps.append(now - last[0])
        last[0] = now
        app.after(15, tick)

    app.after(15, tick)
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if not app.controller.is_running and app.controller.state != ScanState.IDLE:
            settle = time.time() + 0.5
            while time.time() < settle:
                app.update()
            break
        time.sleep(0.003)
    return gaps


def test_ui_scan_responsive_and_filtered(app, tmp_path: Path) -> None:
    root = tmp_path / "data"
    for d in range(30):
        for f in range(200):
            write(root / f"d{d}" / f"f{f}.txt", f % 50 + 1)
    write(root / "big.iso", 3 * MB, fill=b"I")
    write(root / "big_copy.iso", 3 * MB, fill=b"I")
    write(root / "junk.tmp", 10)

    for var in app.drive_vars.values():
        var.set(False)
    app.drive_vars[str(root)] = tk.BooleanVar(value=True)
    app.min_mb_var.set("1")
    for mode, var in app.mode_vars.items():
        var.set(mode in ("large_files", "duplicates", "temp"))
    app.force_var.set(True)
    app.show_page("analysis")
    app.update()

    t0 = time.perf_counter()
    app._start_scan()
    assert time.perf_counter() - t0 < 0.2
    gaps = run_until_done(app)

    assert app.controller.state == ScanState.COMPLETED
    assert sorted(i.name for i in app.items) == ["big.iso", "big_copy.iso", "junk.tmp"]
    assert max(gaps) < 0.5, f"UI thread blocked for {max(gaps) * 1000:.0f} ms"

    analysis_table = app.tables[0]
    assert len(analysis_table.tree.get_children()) == 3
    dup_table = app.tables[1]
    assert len(dup_table.tree.get_children()) == 2

    dup_table._select_dup_copies()
    selected = [i for i in app.items if i.path in app.selected]
    assert len(selected) == 1 and not selected[0].is_primary_in_group

    app.clear_selection()
    analysis_table._select_page()
    assert all(not i.is_primary_in_group for i in app.items if i.path in app.selected)

    analysis_table.search_var.set("junk")
    analysis_table.refresh(reset_page=True)
    assert [analysis_table._row_items[r].name for r in analysis_table.tree.get_children()] == ["junk.tmp"]
    assert analysis_table._row_items[analysis_table.tree.get_children()[0]].safety == SafetyLevel.SAFE.value


def test_pagination_renders_only_one_page(app) -> None:
    from app.core.models import ScanItem

    app.items = [
        ScanItem(path=f"Z:\\many\\file_{i}.bin", size=i, mtime=0, reason="large_file", drive="Z:\\")
        for i in range(5000)
    ]
    table = app.tables[0]
    t0 = time.perf_counter()
    table.refresh(reset_page=True)
    assert time.perf_counter() - t0 < 1.0
    assert len(table.tree.get_children()) == app.page_size
    first = table._row_items[table.tree.get_children()[0]]
    assert first.size == 4999  # default sort: size descending
    table._go_page(1)
    assert table._row_items[table.tree.get_children()[0]].size == 4999 - app.page_size


def test_remove_all_duplicates_keeps_one_per_group(
    app, tmp_path: Path, fake_trash: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "dups"
    for n in range(3):
        write(root / f"a{n}" / "photo.jpg", 5000, fill=b"A")
    for n in range(2):
        write(root / f"b{n}" / "video.mp4", 8000, fill=b"B")
    write(root / "c0" / "doc.pdf", 6000, fill=b"C")
    write(root / "c1" / "doc.pdf", 6000, fill=b"C")
    write(root / "unique.txt", 5000, fill=b"U")

    for var in app.drive_vars.values():
        var.set(False)
    app.drive_vars[str(root)] = tk.BooleanVar(value=True)
    app.min_kb_var.set("1")
    for mode, var in app.mode_vars.items():
        var.set(mode == "duplicates")
    app.force_var.set(True)
    app._start_scan()
    run_until_done(app)
    assert len(app.items) == 7

    # A group whose original vanished must not lose its remaining copy.
    doc_primary = next(i for i in app.items if i.name == "doc.pdf" and i.is_primary_in_group)
    os.remove(doc_primary.path)

    asked: list[str] = []
    monkeypatch.setattr("app.ui.main_window.messagebox.askyesno", lambda *a, **k: asked.append(a[1]) or True)
    monkeypatch.setattr("app.ui.main_window.messagebox.showinfo", lambda *a, **k: None)
    app.remove_all_duplicates()
    end = time.time() + 10
    while app._cleanup_running and time.time() < end:
        app.update()
        time.sleep(0.01)

    assert asked and "3" in asked[0]  # 2 photo copies + 1 video copy
    assert sorted(Path(p).name for p in fake_trash) == ["photo.jpg", "photo.jpg", "video.mp4"]
    assert sum(1 for n in range(3) if (root / f"a{n}" / "photo.jpg").exists()) == 1
    assert sum(1 for n in range(2) if (root / f"b{n}" / "video.mp4").exists()) == 1
    assert sum(1 for n in range(2) if (root / f"c{n}" / "doc.pdf").exists()) == 1
    assert (root / "unique.txt").exists()
    assert not any(
        "duplicate" in i.categories for i in app.items if i.name in ("photo.jpg", "video.mp4")
    )


def test_category_page_scans_only_its_category_and_keeps_others(app, tmp_path: Path) -> None:
    root = tmp_path / "cat"
    write(root / "movie.mkv", 3 * MB, fill=b"M")
    write(root / "session.tmp", 10)
    write(root / "big_session.tmp", 2 * MB, fill=b"T")
    write(root / "notes.txt", 10)
    (root / "empty").mkdir()

    for var in app.drive_vars.values():
        var.set(False)
    app.drive_vars[str(root)] = tk.BooleanVar(value=True)
    app.min_mb_var.set("1")
    for var in app.mode_vars.values():
        var.set(True)  # the Analysis page selection must not matter for a category scan
    app.force_var.set(True)

    app.show_page("large_files")
    app.category_controls["large_files"]["scan"].invoke()
    run_until_done(app)
    assert app.controller.active_params.modes == ("large_files",)
    assert app._shown_page == "large_files"
    assert sorted(i.name for i in app.items) == ["big_session.tmp", "movie.mkv"]
    assert all(i.categories == ("large_file",) for i in app.items)

    app.show_page("temp")
    app._start_scan(only_mode="temp")
    run_until_done(app)
    by = {i.name: i for i in app.items}
    assert sorted(by) == ["big_session.tmp", "movie.mkv", "session.tmp"]
    assert set(by["big_session.tmp"].categories) == {"large_file", "temp_cache"}
    assert len(app.items) == len({i.path for i in app.items})
    assert "notes.txt" not in by and "empty" not in by
    assert app.category_controls["temp"]["status"].cget("text") == app.t(
        "cat_done", count="2", size=format_size(2 * MB + 10))

    temp_table = app.tables[3]
    temp_table.refresh(reset_page=True)
    assert sorted(temp_table._row_items[r].name for r in temp_table.tree.get_children()) == [
        "big_session.tmp", "session.tmp"]

    # Re-analyzing one category replaces only that category.
    (root / "session.tmp").unlink()
    app._start_scan(only_mode="temp")
    run_until_done(app)
    assert sorted(i.name for i in app.items) == ["big_session.tmp", "movie.mkv"]


def test_all_pages_open(app) -> None:
    from app.ui.main_window import PAGES

    for page in PAGES:
        app.show_page(page)
        app.update()
        assert app._shown_page == page
