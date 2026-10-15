"""The installed app must not gain a network client without this test failing."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# scripts/lock_requirements.py is a maintainer tool. It is not imported by the app
# and PyInstaller does not bundle it (ClearSpace.spec starts at main.py).
BANNED = re.compile(
    r"^\s*(?:import|from)\s+(?:socket|ssl|urllib|http\.client|requests|httpx|aiohttp|ftplib|smtplib|websockets)\b",
    re.MULTILINE,
)


def test_application_code_does_not_import_network_clients() -> None:
    offenders: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if BANNED.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_lockfile_hashes_every_package() -> None:
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    blocks = [b for b in text.split("\n\n") if "==" in b]
    assert len(blocks) >= 10
    for block in blocks:
        assert "--hash=sha256:" in block
        assert ">=" not in block
