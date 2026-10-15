from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.cache import set_cache_db_path  # noqa: E402

DAY = 86400.0


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    db = tmp_path_factory.mktemp("cache") / "clearspace_test.db"
    set_cache_db_path(db)
    yield db
    set_cache_db_path(None)


@pytest.fixture
def fake_trash(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the Recycle Bin call so tests never touch the real Recycle Bin."""
    sent: list[str] = []

    def _fake(path: str) -> None:
        sent.append(path)
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.remove(path)

    monkeypatch.setattr("app.core.trash.send2trash", _fake)
    return sent


def write(path: Path, size: int = 10, *, fill: bytes = b"a", age_days: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((fill * (size // max(1, len(fill)) + 1))[:size])
    if age_days is not None:
        ts = time.time() - age_days * DAY
        os.utime(path, (ts, ts))
    return path
