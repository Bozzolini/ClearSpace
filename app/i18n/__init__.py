from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class I18n:
    def __init__(self, lang: str = "it") -> None:
        self._lang = lang
        self._strings: dict[str, str] = {}
        self.load(lang)

    @property
    def lang(self) -> str:
        return self._lang

    def load(self, lang: str) -> None:
        path = Path(__file__).parent / f"{lang}.json"
        if lang not in ("it", "en") or not path.exists():
            path = Path(__file__).parent / "en.json"
            lang = "en"
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
        self._strings = {str(k): str(v) for k, v in data.items()}
        self._lang = lang

    def t(self, key: str, **kwargs: Any) -> str:
        text = self._strings.get(key, key)
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, ValueError):
                return text
        return text


_i18n = I18n("it")


def get_i18n() -> I18n:
    return _i18n


def t(key: str, **kwargs: Any) -> str:
    return _i18n.t(key, **kwargs)


def set_language(lang: str) -> None:
    _i18n.load(lang)
