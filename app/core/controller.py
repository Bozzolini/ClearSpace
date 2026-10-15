from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.core.engine import scan_drives
from app.core.logging_config import get_logger
from app.core.models import (
    DriveScanResult,
    ScanParameters,
    ScanProgress,
    ScanState,
)

log = get_logger("clearspace.controller")


@dataclass
class ControllerEvent:
    kind: str  # "state", "progress", "drive_done", "finished", "error"
    state: ScanState
    progress: Optional[ScanProgress] = None
    drive_result: Optional[DriveScanResult] = None
    error: str = ""


class ScanController:
    """Thread-safe state machine and background scan coordinator.

    State transitions:
      IDLE -> SCANNING <-> PAUSED -> COMPLETED | CANCELLED | FAILED
    All events are pushed to an internal `queue.Queue` so the GUI thread can
    poll them safely without cross-thread Tkinter calls.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: ScanState = ScanState.IDLE
        self._cancel_event = threading.Event()
        self._run_event = threading.Event()
        self._run_event.set()  # Not paused by default
        self._events: queue.Queue[ControllerEvent] = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._active_params: Optional[ScanParameters] = None
        self._started_at: float = 0.0
        self._paused_accum: float = 0.0
        self._pause_started: float = 0.0
        self._files_seen: int = 0
        self._items_found: int = 0
        self._bytes_found: int = 0

    @property
    def state(self) -> ScanState:
        with self._lock:
            return self._state

    @property
    def active_params(self) -> Optional[ScanParameters]:
        with self._lock:
            return self._active_params

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._state in (ScanState.SCANNING, ScanState.PAUSED)

    def elapsed_seconds(self) -> float:
        with self._lock:
            if self._started_at <= 0:
                return 0.0
            now = time.perf_counter()
            paused = self._paused_accum
            if self._state == ScanState.PAUSED and self._pause_started > 0:
                paused += now - self._pause_started
            return max(0.0, (now - self._started_at) - paused)

    def start_scan(self, params: ScanParameters) -> bool:
        with self._lock:
            if self._state in (ScanState.SCANNING, ScanState.PAUSED):
                return False
            self._state = ScanState.SCANNING
            self._active_params = params
            self._cancel_event.clear()
            self._run_event.set()
            self._started_at = time.perf_counter()
            self._paused_accum = 0.0
            self._pause_started = 0.0
            self._files_seen = 0
            self._items_found = 0
            self._bytes_found = 0

        self._events.put(ControllerEvent(kind="state", state=ScanState.SCANNING))

        t = threading.Thread(
            target=self._run_worker,
            args=(params,),
            name="cs-scan-controller",
            daemon=True,
        )
        self._worker = t
        t.start()
        return True

    def pause(self) -> bool:
        with self._lock:
            if self._state != ScanState.SCANNING:
                return False
            self._state = ScanState.PAUSED
            self._pause_started = time.perf_counter()
            self._run_event.clear()
        self._events.put(ControllerEvent(kind="state", state=ScanState.PAUSED))
        return True

    def resume(self) -> bool:
        with self._lock:
            if self._state != ScanState.PAUSED:
                return False
            if self._pause_started > 0:
                self._paused_accum += time.perf_counter() - self._pause_started
                self._pause_started = 0.0
            self._state = ScanState.SCANNING
            self._run_event.set()
        self._events.put(ControllerEvent(kind="state", state=ScanState.SCANNING))
        return True

    def cancel(self) -> bool:
        with self._lock:
            if self._state not in (ScanState.SCANNING, ScanState.PAUSED):
                return False
            self._cancel_event.set()
            self._run_event.set()  # Unblock any paused worker
        return True

    def wait_if_paused(self) -> None:
        while not self._run_event.wait(timeout=0.05):
            if self._cancel_event.is_set():
                return

    def should_cancel(self) -> bool:
        return self._cancel_event.is_set()

    def poll_events(self, max_events: int = 100) -> list[ControllerEvent]:
        out: list[ControllerEvent] = []
        for _ in range(max_events):
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    def wait_for_completion(self, timeout: Optional[float] = None) -> ScanState:
        """Block until the background worker finishes (useful for tests/CLI)."""
        w = self._worker
        if w is not None:
            w.join(timeout=timeout)
        return self.state

    def _run_worker(self, params: ScanParameters) -> None:
        completed_drives_files = 0
        completed_drives_items = 0
        completed_drives_bytes = 0

        def on_progress(
            path: str,
            seen: int,
            found: int,
            drive: str,
            speed: float,
            from_cache: bool,
        ) -> None:
            prog = ScanProgress(
                state=self.state,
                current_path=path,
                files_seen=completed_drives_files + seen,
                items_found=completed_drives_items + found,
                bytes_found=completed_drives_bytes,
                drive=drive,
                speed=speed,
                elapsed_sec=self.elapsed_seconds(),
                from_cache=from_cache,
            )
            self._events.put(
                ControllerEvent(kind="progress", state=self.state, progress=prog)
            )

        def on_drive_done(result: DriveScanResult) -> None:
            nonlocal completed_drives_files, completed_drives_items, completed_drives_bytes
            completed_drives_files += result.files_seen
            completed_drives_items += len(result.items)
            completed_drives_bytes += sum(i.size for i in result.items)
            self._events.put(
                ControllerEvent(
                    kind="drive_done",
                    state=self.state,
                    drive_result=result,
                )
            )

        try:
            scan_drives(
                list(params.drives),
                list(params.modes),
                params.to_options_dict(),
                use_cache=params.use_cache,
                force=params.force_rescan,
                on_progress=on_progress,
                on_drive_done=on_drive_done,
                should_cancel=self.should_cancel,
                wait_if_paused=self.wait_if_paused,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Scan failed with error: %s", exc)
            with self._lock:
                self._state = ScanState.FAILED
            self._events.put(
                ControllerEvent(
                    kind="error",
                    state=ScanState.FAILED,
                    error=str(exc),
                )
            )
            return

        with self._lock:
            if self._cancel_event.is_set():
                self._state = ScanState.CANCELLED
            else:
                self._state = ScanState.COMPLETED
            final_state = self._state

        self._events.put(ControllerEvent(kind="finished", state=final_state))
