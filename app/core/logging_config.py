from __future__ import annotations

import logging
import os
import re
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
_DATA_DIR: Path | None = None
_LOCKED = False


def _legacy_data_dir() -> Path:
    return Path.home() / ".clearspace"


def get_app_data_dir() -> Path:
    """Per-user, non-roaming data directory (%LOCALAPPDATA%\\ClearSpace).

    The profile root (~) roams with domain profiles, which would copy the file index
    of the whole disk to a server; LocalAppData never leaves this PC.
    """
    global _DATA_DIR
    if _DATA_DIR is not None:
        return _DATA_DIR
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "ClearSpace" if local else _legacy_data_dir()
    legacy = _legacy_data_dir()
    if base != legacy and legacy.is_dir() and not base.exists():
        try:
            shutil.move(str(legacy), str(base))
        except OSError:
            pass
    base.mkdir(parents=True, exist_ok=True)
    _DATA_DIR = base
    _lock_data_dir(base)
    return base


def _redaction_patterns() -> list[tuple[re.Pattern[str], str]]:
    pats: list[tuple[re.Pattern[str], str]] = []
    home = os.environ.get("USERPROFILE") or str(Path.home())
    if home:
        pats.append((re.compile(re.escape(home), re.IGNORECASE), "%USERPROFILE%"))
    # Any Windows profile path, not just this session's (covers other local accounts).
    pats.append((re.compile(r"(?i)[a-z]:\\users\\[^\\]+", re.IGNORECASE), r"%USERPROFILE%"))
    user = os.environ.get("USERNAME", "")
    if len(user) >= 3:
        pats.append((re.compile(rf"\b{re.escape(user)}\b", re.IGNORECASE), "<user>"))
    return pats


class RedactingFormatter(logging.Formatter):
    """Strips the profile path and account name from everything written to the log."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._patterns = _redaction_patterns()

    def redact(self, text: str) -> str:
        for pat, repl in self._patterns:
            text = pat.sub(repl, text)
        return text

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))


def get_log_dir() -> Path:
    return get_app_data_dir() / "logs"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("clearspace")
    if _CONFIGURED:
        return logger

    logger.setLevel(level)
    logger.propagate = False
    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "clearspace.log"

    fmt = RedactingFormatter(
        "%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        fh = RotatingFileHandler(
            log_file, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8", delay=True
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING)
    logger.addHandler(sh)

    _CONFIGURED = True
    _lock_data_dir(get_app_data_dir())
    _lock_data_dir(log_dir)
    return logger


def _lock_data_dir(path: Path) -> None:
    """Owner-only ACL; imported lazily to avoid a cycle with privacy.py."""
    global _LOCKED
    try:
        from app.core.privacy import restrict_to_current_user

        restrict_to_current_user(path)
        _LOCKED = True
    except Exception:  # noqa: BLE001
        pass


def close_log_files() -> None:
    """Release log file handles so the files can be deleted."""
    for h in logging.getLogger("clearspace").handlers:
        if isinstance(h, RotatingFileHandler):
            h.close()


def get_logger(name: str = "clearspace") -> logging.Logger:
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
