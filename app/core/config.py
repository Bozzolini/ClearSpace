from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging_config import get_app_data_dir, get_logger

log = get_logger("clearspace.config")

LANGUAGES: tuple[str, ...] = ("it", "en")
THEMES: tuple[str, ...] = ("Dark", "Light")
MAX_WORKERS_LIMIT = 32
MAX_EXCLUSIONS = 500


@dataclass
class AppConfig:
    language: str = "it"
    theme: str = "Dark"  # "Dark" or "Light"
    min_mb: float = 100.0
    min_kb: float = 64.0
    days_old: float = 365.0
    days_downloads: float = 90.0
    custom_exclusions: list[str] = field(default_factory=list)
    use_cache: bool = True
    max_workers: int = 0  # 0 = adaptive per drive (SSD vs HDD)
    page_size: int = 250


def bounded_float(value: Any, default: float, lo: float = 0.0, hi: float = 1e9) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(f):
        return default
    return min(hi, max(lo, f))


def bounded_workers(value: Any) -> int:
    return int(bounded_float(value, 0.0, 0.0, MAX_WORKERS_LIMIT))


def _clean_exclusions(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw[:MAX_EXCLUSIONS]:
        if isinstance(x, str) and x.strip() and os.path.isabs(x):
            out.append(os.path.normpath(x))
    return out


def _config_path() -> Path:
    return get_app_data_dir() / "config.json"


def load_config() -> AppConfig:
    path = _config_path()
    if not path.exists():
        return AppConfig()
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config root is not an object")
        language = raw.get("language", "it")
        theme = raw.get("theme", "Dark")
        return AppConfig(
            language=language if language in LANGUAGES else "it",
            theme=theme if theme in THEMES else "Dark",
            min_mb=bounded_float(raw.get("min_mb"), 100.0),
            min_kb=bounded_float(raw.get("min_kb"), 64.0),
            days_old=bounded_float(raw.get("days_old"), 365.0, hi=36500.0),
            days_downloads=bounded_float(raw.get("days_downloads"), 90.0, hi=36500.0),
            custom_exclusions=_clean_exclusions(raw.get("custom_exclusions", [])),
            use_cache=raw.get("use_cache", True) is not False,
            max_workers=bounded_workers(raw.get("max_workers", 0)),
            page_size=int(bounded_float(raw.get("page_size"), 250.0, 50.0, 1000.0)),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load config, using defaults: %s", exc)
        return AppConfig()


def save_config(cfg: AppConfig) -> None:
    path = _config_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        from app.core.privacy import restrict_to_current_user

        restrict_to_current_user(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to save config: %s", exc)


def delete_config() -> None:
    try:
        _config_path().unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Failed to delete config: %s", exc)
