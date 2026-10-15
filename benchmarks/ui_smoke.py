"""Launch the real window, run a scan on a synthetic folder and measure UI-thread responsiveness."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.cache import set_cache_db_path  # noqa: E402
from app.core.models import ScanState  # noqa: E402


def build_dataset(root: Path, n_dirs: int = 40, n_files: int = 250) -> None:
    for d in range(n_dirs):
        sub = root / f"dir_{d:03d}"
        sub.mkdir()
        for f in range(n_files):
            (sub / f"file_{f:04d}.txt").write_bytes(b"x" * (f % 97 + 1) + bytes([d % 251]))
    (root / "empty_one").mkdir()
    big = root / "big.bin"
    big.write_bytes(os.urandom(3 * 1024 * 1024))
    (root / "big_copy.bin").write_bytes(big.read_bytes())


def main() -> int:
    from app.ui.main_window import MainWindow

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        set_cache_db_path(tmp_path / "cache.db")
        data = tmp_path / "data"
        data.mkdir()
        build_dataset(data)

        app = MainWindow()
        for page in ("dashboard", "analysis", "duplicates", "large_files", "cleanup", "history", "settings"):
            app.show_page(page)
            app.update()

        import tkinter as tk

        for var in app.drive_vars.values():
            var.set(False)
        app.drive_vars[str(data)] = tk.BooleanVar(value=True)
        app.min_mb_var.set("1")
        app.min_kb_var.set("1")
        app.force_var.set(True)

        app.show_page("analysis")
        app.update()
        gaps: list[float] = []
        last = [time.perf_counter()]

        stamps: list[tuple[float, float]] = []

        def tick() -> None:
            now = time.perf_counter()
            gaps.append(now - last[0])
            stamps.append((now - t0, now - last[0]))
            last[0] = now
            app.after(15, tick)

        slow_polls: list[tuple[float, float]] = []
        orig_poll = app._poll

        def timed_poll() -> None:
            s = time.perf_counter()
            orig_poll()
            d = time.perf_counter() - s
            if d > 0.03:
                slow_polls.append((round(s - t0, 2), round(d * 1000)))

        app._poll = timed_poll  # type: ignore[method-assign]
        t0 = time.perf_counter()
        app.after(15, tick)
        s = time.perf_counter()
        app._start_scan()
        start_ms = (time.perf_counter() - s) * 1000
        while time.perf_counter() - t0 < 60:
            app.update()
            if app.controller.state in (ScanState.COMPLETED, ScanState.FAILED, ScanState.CANCELLED) and not app.controller.is_running:
                # let the poll loop drain remaining events
                end = time.perf_counter() + 0.5
                while time.perf_counter() < end:
                    app.update()
                break
            time.sleep(0.005)
        state = app.controller.state
        cats: dict[str, int] = {}
        for it in app.items:
            for c in it.categories:
                cats[c] = cats.get(c, 0) + 1
        app.show_page("duplicates")
        app.update()
        dup_rows = len(app.tables[1].tree.get_children())
        app.show_page("large_files")
        app.update()
        large_rows = len(app.tables[2].tree.get_children())
        max_gap_ms = max(gaps) * 1000 if gaps else 0.0
        print(f"STATE={state.value} ITEMS={len(app.items)} CATS={cats} DUP_ROWS={dup_rows} LARGE_ROWS={large_rows}")
        print(f"UI_TICKS={len(gaps)} MAX_UI_GAP_MS={max_gap_ms:.1f}")
        if os.environ.get("CS_SMOKE_VERBOSE"):
            print("SLOW_TICKS", [(round(a, 2), round(g * 1000)) for a, g in stamps if g > 0.1])
            print("SLOW_POLLS", slow_polls, "START_SCAN_MS", round(start_ms))
        app._on_close()
        set_cache_db_path(None)
        return 0 if state == ScanState.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
