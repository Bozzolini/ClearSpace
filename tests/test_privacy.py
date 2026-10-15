"""Privacy: local-only data, redacted logs, owner-only paths, no PATH-hijacked Explorer."""
from __future__ import annotations

import os
from pathlib import Path

from app.core.cache import list_scan_history, save_drive_index, wipe_all_data
from app.core.exclusions import is_sensitive_norm, normalize_win_path
from app.core.logging_config import RedactingFormatter, get_app_data_dir, get_log_dir
from app.core.models import FileRecord
from app.core.privacy import basename_for_log, erase_local_data, explorer_exe, windows_dir


def test_data_dir_is_localappdata_not_roaming() -> None:
    base = get_app_data_dir()
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        assert str(base).lower().startswith(os.path.normcase(local))
        assert ".clearspace" not in str(base).lower()


def test_logs_redact_any_user_profile() -> None:
    fmt = RedactingFormatter("%(message)s")
    text = fmt.redact(r"scan C:\Users\SomebodyElse\Documents\tax-2024.pdf")
    assert "SomebodyElse" not in text
    assert "tax-2024.pdf" in text


def test_logs_never_keep_full_path() -> None:
    assert basename_for_log(r"C:\Users\Administrator\secret\id_rsa") == "id_rsa"


def test_explorer_is_system32_absolute() -> None:
    exe = explorer_exe()
    assert exe.lower().endswith("explorer.exe")
    assert os.path.normcase(exe).startswith(os.path.normcase(windows_dir()))


def test_sensitive_detection() -> None:
    assert is_sensitive_norm(normalize_win_path(r"C:\tmp\id_rsa"))
    assert is_sensitive_norm(normalize_win_path(r"D:\keys\server.pem"))
    assert is_sensitive_norm(normalize_win_path(r"C:\Users\x\AppData\Local\Google\Chrome\User Data\Default\Login Data"))
    assert not is_sensitive_norm(normalize_win_path(r"C:\tmp\notes.txt"))


def test_erase_local_data_clears_index_and_history(tmp_path: Path) -> None:
    rec = FileRecord(path=str(tmp_path / "secret.txt"), size=1, mtime=1.0, is_dir=False, drive="Z:\\")
    save_drive_index("Z:\\", [rec], is_complete=True)
    wipe_all_data()
    from app.core.cache import load_drive_index

    assert load_drive_index("Z:\\") is None
    assert list_scan_history() == []
    erase_local_data()
    log = get_log_dir() / "clearspace.log"
    # The handler may recreate an empty file; leftover content must not include paths.
    if log.exists():
        assert "secret.txt" not in log.read_text(encoding="utf-8", errors="ignore")
