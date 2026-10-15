from __future__ import annotations

import os
import sys
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk
from typing import Optional

PALETTES: dict[str, dict[str, str]] = {
    "Dark": {
        "bg": "#202020",
        "sidebar": "#1A1A1A",
        "card": "#2B2B2B",
        "card_hover": "#323232",
        "border": "#3A3A3A",
        "text": "#FFFFFF",
        "subtext": "#A6A6A6",
        "accent": "#0078D4",
        "accent_hover": "#1A86D9",
        "cyan": "#00B7C3",
        "success": "#6CCB5F",
        "warning": "#FCE100",
        "danger": "#FF99A4",
        "danger_btn": "#C42B1C",
        "danger_btn_hover": "#D13B2C",
        "row": "#262626",
        "row_alt": "#2C2C2C",
        "select": "#0F3D63",
        "heading": "#303030",
        "nav_active": "#2D2D2D",
        "track": "#3A3A3A",
    },
    "Light": {
        "bg": "#F3F3F3",
        "sidebar": "#EBEBEB",
        "card": "#FFFFFF",
        "card_hover": "#F7F7F7",
        "border": "#E0E0E0",
        "text": "#1B1B1B",
        "subtext": "#5F5F5F",
        "accent": "#0067C0",
        "accent_hover": "#1975C5",
        "cyan": "#008C95",
        "success": "#0F7B0F",
        "warning": "#9D5D00",
        "danger": "#C42B1C",
        "danger_btn": "#C42B1C",
        "danger_btn_hover": "#B02818",
        "row": "#FFFFFF",
        "row_alt": "#F8F8F8",
        "select": "#CCE4F7",
        "heading": "#F0F0F0",
        "nav_active": "#FFFFFF",
        "track": "#E0E0E0",
    },
}

# Segoe Fluent Icons (Win11) / Segoe MDL2 Assets (Win10) share these code points.
ICONS: dict[str, str] = {
    "dashboard": "\uE80F",
    "analysis": "\uE721",
    "duplicates": "\uE8C8",
    "large_files": "\uE7C3",
    "temp": "\uE894",
    "old_files": "\uE823",
    "empty_dirs": "\uE8B7",
    "old_downloads": "\uE896",
    "cleanup": "\uE74D",
    "history": "\uE81C",
    "settings": "\uE713",
    "drive": "\uEDA2",
    "folder": "\uE8B7",
    "play": "\uE768",
    "pause": "\uE769",
    "stop": "\uE71A",
    "refresh": "\uE72C",
    "filter": "\uE71C",
}

_icon_family: Optional[str] = None


def icon_font_family() -> Optional[str]:
    """Return the first available vector icon font, or None (text-only fallback)."""
    global _icon_family
    if _icon_family is not None:
        return _icon_family or None
    try:
        families = set(tkfont.families())
    except Exception:  # noqa: BLE001
        families = set()
    for fam in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if fam in families:
            _icon_family = fam
            return fam
    _icon_family = ""
    return None


def palette(theme: str) -> dict[str, str]:
    return PALETTES.get(theme, PALETTES["Dark"])


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / relative


def app_icon_path() -> Optional[str]:
    for name in ("assets/ClearSpaceIcon3.ico", "assets/ClearSpace.ico"):
        p = resource_path(name)
        if p.exists():
            return os.fspath(p)
    return None


def apply_treeview_style(theme: str) -> None:
    pal = palette(theme)
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:  # noqa: BLE001
        pass
    style.configure(
        "CS.Treeview",
        background=pal["row"],
        fieldbackground=pal["row"],
        foreground=pal["text"],
        rowheight=30,
        borderwidth=0,
        relief="flat",
        font=("Segoe UI", 10),
    )
    style.map(
        "CS.Treeview",
        background=[("selected", pal["select"])],
        foreground=[("selected", pal["text"])],
    )
    style.configure(
        "CS.Treeview.Heading",
        background=pal["heading"],
        foreground=pal["subtext"],
        relief="flat",
        borderwidth=0,
        padding=(8, 6),
        font=("Segoe UI Semibold", 10),
    )
    style.map("CS.Treeview.Heading", background=[("active", pal["card_hover"])])
    style.layout("CS.Treeview", [("CS.Treeview.treearea", {"sticky": "nswe"})])
    style.configure(
        "CS.Vertical.TScrollbar",
        background=pal["card"],
        troughcolor=pal["row"],
        bordercolor=pal["row"],
        arrowcolor=pal["subtext"],
        relief="flat",
    )
