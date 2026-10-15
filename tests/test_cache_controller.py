"""SQLite cache correctness and the scan state machine (pause / resume / cancel / params)."""
from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from app.core import cache
from app.core.controller import ScanController
from app.core.engine import scan_drive
from app.core.models import DriveScanResult, ScanParameters, ScanState

from conftest import write

MB = 1024 * 1024


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    write(root / "big1.bin", 2 * MB)
    write(root / "big2.bin", 4 * MB, fill=b"b")
    write(root / "small.txt", 100)
    write(root / "x.tmp", 100)
    return root


# ----------------------------------------------------------------- cache


def test_fresh_cache_hit_returns_same_results(tree: Path) -> None:
    opts = {"min_mb": 1}
    first = scan_drive(str(tree), ["large_files"], opts, use_cache=True)
    second = scan_drive(str(tree), ["large_files"], opts, use_cache=True)
    assert not first.from_cache and second.from_cache
    assert sorted(i.path for i in first.items) == sorted(i.path for i in second.items)


def test_stale_cache_is_detected_after_modification(tree: Path) -> None:
    opts = {"min_mb": 1}
    scan_drive(str(tree), ["large_files"], opts, use_cache=True)
    (tree / "big1.bin").write_bytes(b"now tiny")
    res = scan_drive(str(tree), ["large_files"], opts, use_cache=True)
    assert not res.from_cache
    assert [i.name for i in res.items] == ["big2.bin"]


def test_stale_cache_is_detected_after_deletion(tree: Path) -> None:
    opts = {"min_mb": 1}
    scan_drive(str(tree), ["large_files"], opts, use_cache=True)
    (tree / "big2.bin").unlink()
    res = scan_drive(str(tree), ["large_files"], opts, use_cache=True)
    assert [i.name for i in res.items] == ["big1.bin"]


def test_new_thresholds_refilter_index_not_previous_results(tree: Path) -> None:
    scan_drive(str(tree), ["large_files"], {"min_mb": 1}, use_cache=True)
    res = scan_drive(str(tree), ["large_files"], {"min_mb": 3}, use_cache=True)
    assert res.from_cache  # served from the metadata index, no disk walk
    assert [i.name for i in res.items] == ["big2.bin"]


def test_index_is_not_shown_as_results(tree: Path) -> None:
    scan_drive(str(tree), ["large_files"], {"min_mb": 1}, use_cache=True)
    res = scan_drive(str(tree), ["temp"], {"min_mb": 1}, use_cache=True)
    assert res.from_cache
    assert [i.name for i in res.items] == ["x.tmp"]


def test_cancelled_scan_is_never_cached(tree: Path) -> None:
    res = scan_drive(str(tree), ["large_files"], {"min_mb": 1}, use_cache=True, should_cancel=lambda: True)
    assert not res.complete and res.items == []
    assert cache.load_drive_index(str(tree)) is None
    assert cache.load_drive_result(str(tree), ["large_files"], ScanParameters.from_options(
        [str(tree)], ["large_files"], {"min_mb": 1}).to_options_dict()) is None


def test_cache_disabled_writes_nothing(tree: Path) -> None:
    scan_drive(str(tree), ["large_files"], {"min_mb": 1}, use_cache=False)
    assert cache.load_drive_index(str(tree)) is None


def test_expired_cache_is_ignored(tree: Path) -> None:
    scan_drive(str(tree), ["large_files"], {"min_mb": 1}, use_cache=True)
    assert cache.load_drive_index(str(tree), max_age_sec=0.0) is None


def test_large_index_sample_validation_detects_staleness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "many"
    for i in range(60):
        write(root / f"f{i}.txt", 10)
    scan_drive(str(root), ["large_files"], {"min_mb": 1}, use_cache=True)
    monkeypatch.setattr(cache, "INDEX_FULL_VALIDATION_LIMIT", 10)
    monkeypatch.setattr(cache, "INDEX_SAMPLE_SIZE", 60)
    assert cache.load_drive_index(str(root)) is not None
    (root / "f7.txt").unlink()
    assert cache.load_drive_index(str(root)) is None


# ----------------------------------------------------------------- state machine


class FakeScan:
    """Stand-in for engine.scan_drives that runs until cancelled and honours pause."""

    def __init__(self) -> None:
        self.ticks = 0
        self.params_seen: list[dict[str, Any]] = []

    def __call__(
        self,
        drives: list[str],
        modes: list[str],
        options: dict[str, Any],
        *,
        on_progress: Optional[Callable[..., None]] = None,
        on_drive_done: Optional[Callable[[DriveScanResult], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        wait_if_paused: Optional[Callable[[], None]] = None,
        **_: Any,
    ) -> list[DriveScanResult]:
        self.params_seen.append(dict(options))
        deadline = time.time() + 10
        while time.time() < deadline:
            if should_cancel and should_cancel():
                return []
            if wait_if_paused:
                wait_if_paused()
            self.ticks += 1
            if on_progress:
                on_progress("X:\\f", self.ticks, 0, "X:\\", 1.0, False)
            time.sleep(0.005)
        return []


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeScan:
    f = FakeScan()
    monkeypatch.setattr("app.core.controller.scan_drives", f)
    return f


def params(**opts: Any) -> ScanParameters:
    return ScanParameters.from_options(["X:\\"], ["large_files"], opts)


def test_start_is_non_blocking_and_state_flow(fake: FakeScan) -> None:
    c = ScanController()
    assert c.state == ScanState.IDLE
    t0 = time.perf_counter()
    assert c.start_scan(params())
    assert time.perf_counter() - t0 < 0.05
    assert c.state == ScanState.SCANNING
    wait_for(lambda: fake.ticks > 3)
    assert c.cancel()
    assert c.wait_for_completion(3) == ScanState.CANCELLED
    kinds = [e.kind for e in c.poll_events(10_000)]
    assert kinds[0] == "state" and kinds[-1] == "finished"


def test_pause_stops_progress_and_resume_continues(fake: FakeScan) -> None:
    c = ScanController()
    c.start_scan(params())
    wait_for(lambda: fake.ticks > 3)
    assert c.pause()
    assert c.state == ScanState.PAUSED
    time.sleep(0.1)
    frozen = fake.ticks
    time.sleep(0.3)
    assert fake.ticks <= frozen + 1
    elapsed_paused = c.elapsed_seconds()
    time.sleep(0.2)
    assert abs(c.elapsed_seconds() - elapsed_paused) < 0.05  # paused time is not counted
    assert c.resume()
    wait_for(lambda: fake.ticks > frozen + 5)
    c.cancel()
    assert c.wait_for_completion(3) == ScanState.CANCELLED


def test_cancel_while_paused(fake: FakeScan) -> None:
    c = ScanController()
    c.start_scan(params())
    wait_for(lambda: fake.ticks > 1)
    c.pause()
    assert c.cancel()
    assert c.wait_for_completion(3) == ScanState.CANCELLED


def test_invalid_transitions_are_rejected(fake: FakeScan) -> None:
    c = ScanController()
    assert not c.pause() and not c.resume() and not c.cancel()
    c.start_scan(params())
    assert not c.resume()
    assert not c.start_scan(params(min_mb=5))  # no second concurrent scan
    c.cancel()
    c.wait_for_completion(3)
    assert c.start_scan(params())  # a finished controller can start again
    c.cancel()
    c.wait_for_completion(3)


def test_parameters_are_immutable_during_scan(fake: FakeScan) -> None:
    p = params(min_mb=100)
    c = ScanController()
    c.start_scan(p)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.min_mb = 1  # type: ignore[misc]
    assert not c.start_scan(params(min_mb=1))
    c.cancel()
    c.wait_for_completion(3)
    assert fake.params_seen == [p.to_options_dict()]


def test_failure_becomes_failed_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> list[DriveScanResult]:
        raise RuntimeError("disk vanished")

    monkeypatch.setattr("app.core.controller.scan_drives", boom)
    c = ScanController()
    c.start_scan(params())
    assert c.wait_for_completion(3) == ScanState.FAILED
    errors = [e for e in c.poll_events(100) if e.kind == "error"]
    assert errors and "disk vanished" in errors[0].error


def test_real_scan_completes_and_emits_results(tree: Path) -> None:
    c = ScanController()
    p = ScanParameters.from_options([str(tree)], ["large_files", "temp"], {"min_mb": 1, "use_cache": False})
    c.start_scan(p)
    assert c.wait_for_completion(30) == ScanState.COMPLETED
    done = [e for e in c.poll_events(10_000) if e.kind == "drive_done"]
    assert len(done) == 1
    assert sorted(i.name for i in done[0].drive_result.items) == ["big1.bin", "big2.bin", "x.tmp"]


def test_real_scan_cancel_is_prompt(tmp_path: Path) -> None:
    root = tmp_path / "bulk"
    for d in range(30):
        for f in range(100):
            write(root / f"a{d}" / f"b{f % 5}" / f"f{f}.txt", 10)
    c = ScanController()
    c.start_scan(ScanParameters.from_options([str(root)], ["old_files"], {"use_cache": True, "force_rescan": True}))
    threading.Timer(0.01, c.cancel).start()
    t0 = time.perf_counter()
    final = c.wait_for_completion(10)
    assert time.perf_counter() - t0 < 5
    assert final in (ScanState.CANCELLED, ScanState.COMPLETED)
    if final == ScanState.CANCELLED:
        assert cache.load_drive_index(str(root)) is None
