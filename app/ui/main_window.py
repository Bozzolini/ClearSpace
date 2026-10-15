from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Iterable, Optional

import customtkinter as ctk

from app.core.cache import clear_drive_cache, list_scan_history
from app.core.classifier import ALL_MODES, MODE_TO_CATEGORY
from app.core.config import (
    AppConfig,
    bounded_float,
    bounded_workers,
    load_config,
    save_config,
)
from app.core.controller import ControllerEvent, ScanController
from app.core.disk import format_size, get_drive_details, list_drives
from app.core.exclusions import normalize_win_path
from app.core.logging_config import get_logger, setup_logging
from app.core.models import (
    CleanupResult,
    DriveInfo,
    SafetyLevel,
    ScanItem,
    ScanParameters,
    ScanProgress,
    ScanState,
)
from app.core.trash import clean_items
from app.i18n import get_i18n, set_language
from app.ui.theme import ICONS, app_icon_path, apply_treeview_style, icon_font_family, palette
from app.core.privacy import erase_local_data, open_in_explorer
from app.ui.widgets import ActivityBar, Card, ResultsTable, format_mtime

log = get_logger("clearspace.ui")

PAGES: tuple[str, ...] = (
    "dashboard",
    "analysis",
    "duplicates",
    "large_files",
    "temp",
    "old_files",
    "empty_dirs",
    "old_downloads",
    "cleanup",
    "history",
    "settings",
)

# Category pages are keyed by their scan mode; each can analyze just its own mode.
CATEGORY_PAGES: tuple[str, ...] = ("duplicates", "large_files", "temp", "old_files", "empty_dirs", "old_downloads")

NAV_KEYS: dict[str, str] = {
    "dashboard": "nav_dashboard",
    "analysis": "nav_analysis",
    "duplicates": "nav_duplicates",
    "large_files": "nav_large_files",
    "temp": "nav_temp",
    "old_files": "nav_old_files",
    "empty_dirs": "nav_empty_dirs",
    "old_downloads": "nav_old_downloads",
    "cleanup": "nav_cleanup",
    "history": "nav_history",
    "settings": "nav_settings",
}

DEFAULT_MODES: tuple[str, ...] = ("large_files", "duplicates", "temp", "empty_dirs", "old_downloads")

POLL_MS = 80
TABLE_REFRESH_MS = 400


def format_elapsed(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _merged_safety(item: ScanItem) -> str:
    """Same rule as the classifier: the kept duplicate copy is never 'safe'."""
    if item.is_primary_in_group and "duplicate" in item.categories:
        return SafetyLevel.CANDIDATE.value
    if any(c in ("temp_cache", "empty_dir") for c in item.categories) or (
        "duplicate" in item.categories and not item.is_primary_in_group
    ):
        return SafetyLevel.SAFE.value
    return SafetyLevel.CANDIDATE.value


def _parse_float(raw: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(raw.replace(",", ".").strip())
    except (ValueError, AttributeError):
        return default
    return bounded_float(value, default, lo=minimum)


class MainWindow(ctk.CTk):
    def __init__(self) -> None:
        self.config_data: AppConfig = load_config()
        ctk.set_appearance_mode(self.config_data.theme)
        ctk.set_default_color_theme("blue")
        super().__init__()
        set_language(self.config_data.language)

        self.controller = ScanController()
        self.items: list[ScanItem] = []
        self.selected: set[str] = set()
        self._items_by_path: dict[str, ScanItem] = {}
        self._drive_infos: dict[str, DriveInfo] = {}
        self._custom_roots: list[str] = []
        self._last_progress: Optional[ScanProgress] = None
        self._last_result_params: Optional[ScanParameters] = None
        self._table_refresh_job: Optional[str] = None
        self._bg_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._cleanup_running = False
        self._current_page = "dashboard"
        self._shown_page: Optional[str] = None
        self._finished_modes: tuple[str, ...] = ()
        self._fonts: dict[tuple[int, str], ctk.CTkFont] = {}

        self.drive_vars: dict[str, tk.BooleanVar] = {}
        self.mode_vars: dict[str, tk.BooleanVar] = {
            m: tk.BooleanVar(value=m in DEFAULT_MODES) for m in ALL_MODES
        }
        cfg = self.config_data
        self.min_mb_var = tk.StringVar(value=f"{cfg.min_mb:g}")
        self.days_old_var = tk.StringVar(value=f"{cfg.days_old:g}")
        self.days_dl_var = tk.StringVar(value=f"{cfg.days_downloads:g}")
        self.min_kb_var = tk.StringVar(value=f"{cfg.min_kb:g}")
        self.force_var = tk.BooleanVar(value=False)
        for var in (self.min_mb_var, self.days_old_var, self.days_dl_var, self.min_kb_var):
            var.trace_add("write", lambda *_: self._on_params_changed())
        for var in self.mode_vars.values():
            var.trace_add("write", lambda *_: self._on_params_changed())

        self.title("ClearSpace")
        self.geometry("1360x860")
        self.minsize(1100, 700)
        icon = app_icon_path()
        if icon:
            try:
                self.iconbitmap(icon)
            except tk.TclError:
                pass

        for root in list_drives():
            self.drive_vars[root] = tk.BooleanVar(value=root.upper().startswith("C"))

        self._build_ui()
        self._load_drive_details_async()
        self.after(POLL_MS, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ helpers

    @property
    def theme(self) -> str:
        return self.config_data.theme if self.config_data.theme in ("Dark", "Light") else "Dark"

    @property
    def pal(self) -> dict[str, str]:
        return palette(self.theme)

    @property
    def page_size(self) -> int:
        return max(50, min(1000, int(self.config_data.page_size)))

    def t(self, key: str, **kw: Any) -> str:
        return get_i18n().t(key, **kw)

    def _font(self, size: int = 13, weight: str = "normal") -> ctk.CTkFont:
        key = (size, weight)
        font = self._fonts.get(key)
        if font is None:
            font = ctk.CTkFont(family="Segoe UI", size=size, weight=weight)
            self._fonts[key] = font
        return font

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        primary: bool = False,
        danger: bool = False,
        width: int = 0,
        **kw: Any,
    ) -> ctk.CTkButton:
        pal = self.pal
        if danger:
            colors = dict(fg_color=pal["danger_btn"], hover_color=pal["danger_btn_hover"], text_color="#FFFFFF")
        elif primary:
            colors = dict(fg_color=pal["accent"], hover_color=pal["accent_hover"], text_color="#FFFFFF")
        else:
            colors = dict(
                fg_color=pal["card"], hover_color=pal["card_hover"], text_color=pal["text"],
                border_width=1, border_color=pal["border"],
            )
        opts: dict[str, Any] = dict(height=34, corner_radius=8, font=self._font(13), **colors)
        if width:
            opts["width"] = width
        opts.update(kw)
        return ctk.CTkButton(parent, text=text, command=command, **opts)

    def _page_header(self, parent: tk.Misc, title_key: str, subtitle_key: str) -> None:
        pal = self.pal
        ctk.CTkLabel(
            parent, text=self.t(title_key), font=self._font(26, "bold"), text_color=pal["text"], anchor="w"
        ).pack(fill="x")
        ctk.CTkLabel(
            parent, text=self.t(subtitle_key), font=self._font(13), text_color=pal["subtext"],
            anchor="w", justify="left", wraplength=1000,
        ).pack(fill="x", pady=(2, 16))

    def _section_label(self, parent: tk.Misc, key: str) -> None:
        ctk.CTkLabel(
            parent, text=self.t(key), font=self._font(14, "bold"), text_color=self.pal["text"], anchor="w"
        ).pack(fill="x", padx=16, pady=(14, 6))

    # ------------------------------------------------------------------ layout

    def _build_ui(self) -> None:
        for child in list(self.winfo_children()):
            child.destroy()
        self._shown_page = None
        pal = self.pal
        self.configure(fg_color=pal["bg"])
        apply_treeview_style(self.theme)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()

        self.content = ctk.CTkFrame(self, fg_color=pal["bg"], corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.status_bar = ctk.CTkLabel(
            self, text=self.t("status_ready_bar"), anchor="w", font=self._font(12),
            text_color=pal["subtext"], fg_color=pal["sidebar"], height=28,
        )
        self.status_bar.grid(row=1, column=0, columnspan=2, sticky="ew")

        self.pages: dict[str, ctk.CTkFrame] = {}
        builders: dict[str, Callable[[ctk.CTkFrame], None]] = {
            "dashboard": self._build_dashboard,
            "analysis": self._build_analysis,
            "cleanup": self._build_cleanup,
            "history": self._build_history,
            "settings": self._build_settings,
        }
        for mode in CATEGORY_PAGES:
            builders[mode] = lambda frame, m=mode: self._build_category_page(frame, m)
        self.tables: list[ResultsTable] = []
        self.category_controls: dict[str, dict[str, Any]] = {}
        for key in PAGES:
            frame = ctk.CTkFrame(self.content, fg_color=pal["bg"], corner_radius=0)
            frame.grid(row=0, column=0, sticky="nsew", padx=28, pady=22)
            builders[key](frame)
            frame.grid_remove()
            self.pages[key] = frame
        self.show_page(self._current_page)
        self._apply_state(self.controller.state)
        self._refresh_tables()

    def _build_sidebar(self) -> None:
        pal = self.pal
        side = ctk.CTkFrame(self, fg_color=pal["sidebar"], corner_radius=0, width=230)
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)

        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(22, 18))
        ctk.CTkLabel(brand, text="ClearSpace", font=self._font(20, "bold"), text_color=pal["text"]).pack(anchor="w")
        ctk.CTkLabel(brand, text="Windows 10 / 11", font=self._font(11), text_color=pal["cyan"]).pack(anchor="w")

        icon_family = icon_font_family()
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self.nav_indicators: dict[str, ctk.CTkFrame] = {}
        for key in PAGES:
            row = ctk.CTkFrame(side, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=2)
            indicator = ctk.CTkFrame(row, width=3, height=18, corner_radius=2, fg_color="transparent")
            indicator.pack(side="left", padx=(0, 4))
            glyph = ICONS.get(key, "")
            label = self.t(NAV_KEYS[key])
            btn = ctk.CTkButton(
                row,
                text=label,
                anchor="w",
                height=38,
                corner_radius=6,
                fg_color="transparent",
                hover_color=pal["nav_active"],
                text_color=pal["text"],
                font=self._font(13),
                command=lambda k=key: self.show_page(k),
            )
            if icon_family and glyph:
                ic = ctk.CTkLabel(row, text=glyph, width=26, font=ctk.CTkFont(family=icon_family, size=15),
                                  text_color=pal["cyan"])
                ic.pack(side="left", padx=(2, 0))
                ic.bind("<Button-1>", lambda _e, k=key: self.show_page(k))
            btn.pack(side="left", fill="x", expand=True)
            self.nav_buttons[key] = btn
            self.nav_indicators[key] = indicator

        footer = ctk.CTkFrame(side, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=16, pady=16)
        self.side_state = ctk.CTkLabel(footer, text="", font=self._font(12), text_color=pal["subtext"], anchor="w")
        self.side_state.pack(fill="x")

    def show_page(self, key: str) -> None:
        if key not in self.pages:
            return
        if key != self._shown_page:
            pal = self.pal
            previous = self._shown_page
            if previous in self.pages:
                self.pages[previous].grid_remove()
                self.nav_buttons[previous].configure(fg_color="transparent", font=self._font(13))
                self.nav_indicators[previous].configure(fg_color="transparent")
            self.pages[key].grid()
            self.nav_buttons[key].configure(fg_color=pal["nav_active"], font=self._font(13, "bold"))
            self.nav_indicators[key].configure(fg_color=pal["accent"])
            self._shown_page = key
        self._current_page = key
        if key == "cleanup":
            self._refresh_cleanup_page()
        elif key == "history":
            self._refresh_history()
        elif key in CATEGORY_PAGES:
            self._refresh_category_drives(key)
        elif key == "dashboard":
            self._refresh_dashboard()

    # ------------------------------------------------------------------ dashboard

    def _build_dashboard(self, page: ctk.CTkFrame) -> None:
        pal = self.pal
        self._page_header(page, "dash_title", "dash_subtitle")

        stats = ctk.CTkFrame(page, fg_color="transparent")
        stats.pack(fill="x")
        self.dash_stat_labels: dict[str, ctk.CTkLabel] = {}
        for idx, key in enumerate(("dash_total_space", "dash_used_space", "dash_free_space",
                                   "dash_last_scan", "dash_recoverable", "dash_safe_recoverable")):
            card = Card(stats, self)
            card.grid(row=idx // 3, column=idx % 3, sticky="nsew", padx=(0, 12), pady=(0, 12))
            stats.grid_columnconfigure(idx % 3, weight=1)
            ctk.CTkLabel(card, text=self.t(key), font=self._font(12), text_color=pal["subtext"], anchor="w").pack(
                fill="x", padx=16, pady=(14, 0))
            val = ctk.CTkLabel(card, text="-", font=self._font(22, "bold"),
                               text_color=pal["cyan"] if idx >= 4 else pal["text"], anchor="w")
            val.pack(fill="x", padx=16, pady=(2, 14))
            self.dash_stat_labels[key] = val

        drives_card = Card(page, self)
        drives_card.pack(fill="both", expand=True, pady=(4, 0))
        head = ctk.CTkFrame(drives_card, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=self.t("dash_drives_section"), font=self._font(14, "bold"),
                     text_color=pal["text"]).pack(side="left", padx=16, pady=(14, 6))
        self._button(head, self.t("dash_quick_scan"), self._start_scan, primary=True).pack(
            side="right", padx=16, pady=(12, 4))
        self._button(head, self.t("dash_go_analysis"), lambda: self.show_page("analysis")).pack(
            side="right", pady=(12, 4))
        self._button(head, self.t("dash_refresh_drives"), self._load_drive_details_async).pack(
            side="right", padx=8, pady=(12, 4))
        self.dash_drives = ctk.CTkScrollableFrame(drives_card, fg_color="transparent")
        self.dash_drives.pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def _refresh_dashboard(self) -> None:
        if not hasattr(self, "dash_drives"):
            return
        pal = self.pal
        for child in self.dash_drives.winfo_children():
            child.destroy()
        infos = [self._drive_infos[r] for r in sorted(self._drive_infos)]
        total = sum(i.total_bytes for i in infos)
        used = sum(i.used_bytes for i in infos)
        free = sum(i.free_bytes for i in infos)
        for info in infos:
            row = ctk.CTkFrame(self.dash_drives, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=6)
            title = f"{info.root.rstrip(chr(92))}  {info.label or ''}  ·  {info.fs_type}  ·  {'SSD' if info.is_ssd else 'HDD'}"
            ctk.CTkLabel(row, text=title, font=self._font(13, "bold"), text_color=pal["text"], anchor="w").pack(fill="x")
            bar = ctk.CTkProgressBar(row, height=10, corner_radius=5, progress_color=pal["accent"] if info.used_ratio < 0.9 else pal["danger_btn"], fg_color=pal["track"])
            bar.pack(fill="x", pady=4)
            bar.set(info.used_ratio)
            ctk.CTkLabel(
                row,
                text=self.t("drive_free_of", free=format_size(info.free_bytes), total=format_size(info.total_bytes),
                            used_pct=int(round(info.used_ratio * 100))),
                font=self._font(12), text_color=pal["subtext"], anchor="w",
            ).pack(fill="x")

        if self.items:
            found = len(self.items)
            recoverable = sum(i.size for i in self.items if not i.is_primary_in_group)
        else:
            history = list_scan_history(limit=1)
            found = history[0].items_found if history else 0
            recoverable = history[0].potential_bytes if history else 0
        safe = sum(
            i.size for i in self.items if i.safety == SafetyLevel.SAFE.value and not i.is_primary_in_group
        )
        values = {
            "dash_total_space": format_size(total) if infos else "-",
            "dash_used_space": format_size(used) if infos else "-",
            "dash_free_space": format_size(free) if infos else "-",
            "dash_last_scan": f"{found:,}".replace(",", "."),
            "dash_recoverable": format_size(recoverable),
            "dash_safe_recoverable": format_size(safe),
        }
        for key, lbl in self.dash_stat_labels.items():
            lbl.configure(text=values[key])

    def _load_drive_details_async(self) -> None:
        def _job() -> None:
            try:
                self._bg_queue.put(("drives", get_drive_details()))
            except Exception as exc:  # noqa: BLE001
                log.warning("Drive details failed: %s", exc)

        threading.Thread(target=_job, name="cs-drive-info", daemon=True).start()

    # ------------------------------------------------------------------ analysis

    def _build_analysis(self, page: ctk.CTkFrame) -> None:
        pal = self.pal
        self._page_header(page, "nav_analysis", "subtitle")

        body = ctk.CTkFrame(page, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(1, weight=1)

        config_panel = ctk.CTkScrollableFrame(body, fg_color=pal["card"], corner_radius=10, width=300,
                                              border_width=1, border_color=pal["border"])
        config_panel.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(0, 16))

        self._section_label(config_panel, "drives_group")
        self.drives_box = ctk.CTkFrame(config_panel, fg_color="transparent")
        self.drives_box.pack(fill="x", padx=12)
        self._render_drive_checks()
        row = ctk.CTkFrame(config_panel, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(6, 0))
        self._button(row, self.t("select_all_drives"), lambda: self._set_all_drives(True), width=70, height=28).pack(side="left")
        self._button(row, self.t("deselect_all_drives"), lambda: self._set_all_drives(False), width=70, height=28).pack(side="left", padx=6)
        self._button(row, self.t("add_folder"), self._add_custom_root, width=110, height=28).pack(side="left")

        self._section_label(config_panel, "modes_group")
        for mode in ALL_MODES:
            ctk.CTkCheckBox(
                config_panel, text=self.t(f"mode_{mode}"), variable=self.mode_vars[mode],
                font=self._font(13), text_color=pal["text"], corner_radius=5,
                fg_color=pal["accent"], hover_color=pal["accent_hover"],
            ).pack(fill="x", padx=16, pady=3)
        self._button(config_panel, self.t("select_all_modes"), self._select_all_modes, height=28).pack(
            anchor="w", padx=16, pady=(6, 0))

        self._section_label(config_panel, "options_group")
        for key, var in (("opt_min_mb", self.min_mb_var), ("opt_days_old", self.days_old_var),
                         ("opt_days_downloads", self.days_dl_var), ("opt_min_dup_kb", self.min_kb_var)):
            r = ctk.CTkFrame(config_panel, fg_color="transparent")
            r.pack(fill="x", padx=16, pady=3)
            ctk.CTkLabel(r, text=self.t(key), font=self._font(12), text_color=pal["subtext"], anchor="w").pack(
                side="left", fill="x", expand=True)
            ctk.CTkEntry(r, textvariable=var, width=80, height=28, corner_radius=6).pack(side="right")
        ctk.CTkCheckBox(
            config_panel, text=self.t("rescan_drive"), variable=self.force_var, font=self._font(12),
            text_color=pal["text"], fg_color=pal["accent"], hover_color=pal["accent_hover"],
        ).pack(fill="x", padx=16, pady=(10, 4))
        self.reapply_btn = self._button(config_panel, self.t("btn_reapply_filters"), self._reapply_filters, height=30)
        self.reapply_btn.pack(fill="x", padx=16, pady=(6, 4))
        self.params_hint = ctk.CTkLabel(config_panel, text="", font=self._font(11), text_color=pal["warning"],
                                        wraplength=250, justify="left", anchor="w")
        self.params_hint.pack(fill="x", padx=16, pady=(0, 12))

        status = Card(body, self)
        status.grid(row=0, column=1, sticky="ew", pady=(0, 12))
        ctrl = ctk.CTkFrame(status, fg_color="transparent")
        ctrl.pack(fill="x", padx=16, pady=(14, 8))
        self.scan_btn = self._button(ctrl, self.t("btn_scan"), self._start_scan, primary=True, width=150)
        self.scan_btn.pack(side="left")
        self.pause_btn = self._button(ctrl, self.t("btn_pause"), self._toggle_pause, width=120)
        self.pause_btn.pack(side="left", padx=8)
        self.cancel_btn = self._button(ctrl, self.t("btn_cancel"), self._cancel_scan, danger=True, width=120)
        self.cancel_btn.pack(side="left")

        self.progress = ActivityBar(status, track=pal["track"], color=pal["cyan"], bg=pal["card"])
        self.progress.pack(fill="x", padx=16, pady=(4, 10))
        self.progress.set(0)

        stats = ctk.CTkFrame(status, fg_color="transparent")
        stats.pack(fill="x", padx=16, pady=(0, 6))
        self.stat_labels: dict[str, ctk.CTkLabel] = {}
        for col, key in enumerate(("stat_state", "stat_files_seen", "stat_results_found",
                                   "stat_current_drive", "stat_elapsed")):
            stats.grid_columnconfigure(col, weight=1)
            cell = ctk.CTkFrame(stats, fg_color="transparent")
            cell.grid(row=0, column=col, sticky="w")
            ctk.CTkLabel(cell, text=self.t(key), font=self._font(11), text_color=pal["subtext"]).pack(anchor="w")
            lbl = ctk.CTkLabel(cell, text="-", font=self._font(17, "bold"), text_color=pal["text"])
            lbl.pack(anchor="w")
            self.stat_labels[key] = lbl
        self.path_label = ctk.CTkLabel(status, text="", font=self._font(11), text_color=pal["subtext"], anchor="w")
        self.path_label.pack(fill="x", padx=16, pady=(0, 12))

        table = ResultsTable(body, self)
        table.grid(row=1, column=1, sticky="nsew")
        self.tables.append(table)

    def _render_drive_checks(self) -> None:
        pal = self.pal
        for child in self.drives_box.winfo_children():
            child.destroy()
        for root in sorted(self.drive_vars):
            info = self._drive_infos.get(root)
            label = root.rstrip("\\")
            if info is not None:
                label += f"  {info.label}  ({format_size(info.free_bytes)} / {format_size(info.total_bytes)})"
            elif root in self._custom_roots:
                label = root
            ctk.CTkCheckBox(
                self.drives_box, text=label, variable=self.drive_vars[root], font=self._font(12),
                text_color=pal["text"], fg_color=pal["accent"], hover_color=pal["accent_hover"],
            ).pack(fill="x", pady=2)

    def _set_all_drives(self, value: bool) -> None:
        for var in self.drive_vars.values():
            var.set(value)

    def _select_all_modes(self) -> None:
        for var in self.mode_vars.values():
            var.set(True)

    def _add_custom_root(self) -> None:
        folder = filedialog.askdirectory(parent=self)
        if not folder:
            return
        folder = os.path.normpath(folder)
        if folder not in self.drive_vars:
            self.drive_vars[folder] = tk.BooleanVar(value=True)
            self._custom_roots.append(folder)
        else:
            self.drive_vars[folder].set(True)
        self._render_drive_checks()

    def _collect_params(
        self,
        *,
        force: Optional[bool] = None,
        use_cache: Optional[bool] = None,
        only_mode: Optional[str] = None,
    ) -> ScanParameters:
        cfg = self.config_data
        drives = [d for d, v in self.drive_vars.items() if v.get()]
        modes = [only_mode] if only_mode else [m for m in ALL_MODES if self.mode_vars[m].get()]
        return ScanParameters.from_options(
            drives,
            modes,
            {
                "min_mb": _parse_float(self.min_mb_var.get(), cfg.min_mb),
                "min_kb": _parse_float(self.min_kb_var.get(), cfg.min_kb),
                "days_old": _parse_float(self.days_old_var.get(), cfg.days_old),
                "days_downloads": _parse_float(self.days_dl_var.get(), cfg.days_downloads),
                "custom_exclusions": list(cfg.custom_exclusions),
                "use_cache": cfg.use_cache if use_cache is None else use_cache,
                "force_rescan": self.force_var.get() if force is None else force,
                "max_workers": cfg.max_workers,
            },
        )

    def _start_scan(
        self,
        *,
        force: Optional[bool] = None,
        use_cache: Optional[bool] = None,
        only_mode: Optional[str] = None,
    ) -> None:
        if self.controller.is_running or self._cleanup_running:
            return
        params = self._collect_params(force=force, use_cache=use_cache, only_mode=only_mode)
        if not params.drives:
            self._set_status(self.t("status_Select_drive"))
            self.show_page("analysis")
            return
        if not params.modes:
            self._set_status(self.t("status_select_mode"))
            self.show_page("analysis")
            return
        if only_mode:
            self._forget_category(MODE_TO_CATEGORY[only_mode])
        else:
            self.items = []
            self._items_by_path = {}
            self.selected.clear()
        self._last_progress = None
        if self.controller.start_scan(params):
            if only_mode is None:
                self._last_result_params = params
                self.params_hint.configure(text="")
                self.show_page("analysis")
            self._refresh_tables()

    def _forget_category(self, category: str) -> None:
        """Drop one category from the current results before re-analyzing just that category."""
        kept: list[ScanItem] = []
        for it in self.items:
            if category in it.categories:
                it.categories = tuple(c for c in it.categories if c != category)
                if category == "duplicate":
                    it.group_id = None
                    it.is_primary_in_group = False
                if not it.categories:
                    self.selected.discard(it.path)
                    continue
                it.reason = it.categories[0]
                it.safety = _merged_safety(it)
            kept.append(it)
        self.items = kept
        self._items_by_path = {normalize_win_path(i.path): i for i in self.items}

    def _reapply_filters(self) -> None:
        # Re-filters the SQLite metadata index with the new thresholds; walks the disk only when no index exists.
        self._start_scan(force=False, use_cache=True)

    def _toggle_pause(self) -> None:
        if self.controller.state == ScanState.PAUSED:
            self.controller.resume()
        else:
            self.controller.pause()

    def _cancel_scan(self) -> None:
        self.controller.cancel()

    def _on_params_changed(self) -> None:
        if not hasattr(self, "params_hint"):
            return
        if self.controller.is_running:
            self.params_hint.configure(text=self.t("status_param_changed_scanning"))
            return
        if self._last_result_params is None:
            return
        current = self._collect_params(force=False)
        last = self._last_result_params
        changed = (current.modes, current.min_mb, current.min_kb, current.days_old, current.days_downloads) != (
            last.modes, last.min_mb, last.min_kb, last.days_old, last.days_downloads
        )
        self.params_hint.configure(text=self.t("params_changed_hint") if changed else "")

    # ------------------------------------------------------------------ results pages

    def _category_threshold(self, mode: str) -> Optional[tuple[str, tk.StringVar]]:
        return {
            "large_files": ("opt_min_mb", self.min_mb_var),
            "duplicates": ("opt_min_dup_kb", self.min_kb_var),
            "old_files": ("opt_days_old", self.days_old_var),
            "old_downloads": ("opt_days_downloads", self.days_dl_var),
        }.get(mode)

    def _build_category_page(self, page: ctk.CTkFrame, mode: str) -> None:
        pal = self.pal
        self._page_header(page, f"page_title_{mode}", f"page_sub_{mode}")

        bar_card = Card(page, self)
        bar_card.pack(fill="x", pady=(0, 12))
        row = ctk.CTkFrame(bar_card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(12, 6))

        drives_lbl = ctk.CTkLabel(row, text="", font=self._font(13, "bold"), text_color=pal["text"], anchor="w")
        drives_lbl.pack(side="left")
        self._button(row, self.t("cat_change_drives"), lambda: self.show_page("analysis"), height=30).pack(
            side="left", padx=(10, 20))

        threshold = self._category_threshold(mode)
        if threshold is not None:
            key, var = threshold
            ctk.CTkLabel(row, text=self.t(key), font=self._font(12), text_color=pal["subtext"]).pack(side="left")
            ctk.CTkEntry(row, textvariable=var, width=80, height=30, corner_radius=6).pack(side="left", padx=(6, 0))

        cancel_btn = self._button(row, self.t("btn_cancel"), self._cancel_scan, danger=True, width=100, height=30)
        cancel_btn.pack(side="right")
        pause_btn = self._button(row, self.t("btn_pause"), self._toggle_pause, width=100, height=30)
        pause_btn.pack(side="right", padx=8)
        scan_btn = self._button(
            row,
            self.t("cat_scan_btn", category=self.t(NAV_KEYS[mode])),
            lambda m=mode: self._start_scan(only_mode=m),
            primary=True,
            height=30,
        )
        scan_btn.pack(side="right")

        bar = ActivityBar(bar_card, track=pal["track"], color=pal["cyan"], bg=pal["card"])
        bar.pack(fill="x", padx=16, pady=(4, 4))
        status_lbl = ctk.CTkLabel(bar_card, text=self.t("cat_hint"), font=self._font(12),
                                  text_color=pal["subtext"], anchor="w")
        status_lbl.pack(fill="x", padx=16, pady=(0, 10))

        self.category_controls[mode] = {
            "drives": drives_lbl, "scan": scan_btn, "pause": pause_btn,
            "cancel": cancel_btn, "bar": bar, "status": status_lbl,
        }
        self._refresh_category_drives(mode)

        table = ResultsTable(
            page, self,
            fixed_category=MODE_TO_CATEGORY[mode],
            group_duplicates=mode == "duplicates",
        )
        table.pack(fill="both", expand=True)
        self.tables.append(table)

    def _refresh_category_drives(self, mode: Optional[str] = None) -> None:
        drives = [d.rstrip("\\") if len(d) <= 3 else d for d, v in sorted(self.drive_vars.items()) if v.get()]
        text = self.t("cat_drives", drives=", ".join(drives)) if drives else self.t("cat_no_drive")
        for m in [mode] if mode else list(self.category_controls):
            ctrl = self.category_controls.get(m)
            if ctrl is not None:
                ctrl["drives"].configure(text=text)

    def _active_modes(self) -> tuple[str, ...]:
        params = self.controller.active_params
        return params.modes if params is not None else ()

    def _refresh_tables(self) -> None:
        self._table_refresh_job = None
        for table in self.tables:
            table.refresh()

    def _schedule_table_refresh(self) -> None:
        if self._table_refresh_job is None:
            self._table_refresh_job = self.after(TABLE_REFRESH_MS, self._refresh_tables)

    def _add_items(self, new_items: Iterable[ScanItem]) -> None:
        for item in new_items:
            key = normalize_win_path(item.path)
            existing = self._items_by_path.get(key)
            if existing is None:
                self._items_by_path[key] = item
                self.items.append(item)
                continue
            # Same file found by another category scan: one row with all its categories.
            existing.categories = existing.categories + tuple(
                c for c in item.categories if c not in existing.categories
            )
            existing.size, existing.mtime = item.size, item.mtime
            if "duplicate" in item.categories:
                existing.group_id = item.group_id
                existing.is_primary_in_group = item.is_primary_in_group
            existing.safety = _merged_safety(existing)
        self._schedule_table_refresh()

    def toggle_selection(self, items: list[ScanItem]) -> None:
        if all(i.path in self.selected for i in items):
            self.set_selected(items, False)
        else:
            self.set_selected(items, True)

    def set_selected(self, items: Iterable[ScanItem], value: bool) -> None:
        for item in items:
            if value:
                self.selected.add(item.path)
            else:
                self.selected.discard(item.path)
            item.selected = value
        self._on_selection_changed()

    def clear_selection(self) -> None:
        for item in self.items:
            item.selected = False
        self.selected.clear()
        self._on_selection_changed()

    def _on_selection_changed(self) -> None:
        for table in self.tables:
            table.refresh_checks()
        if self._current_page == "cleanup":
            self._refresh_cleanup_page()

    def show_details(self, item: ScanItem) -> None:
        pal = self.pal
        win = ctk.CTkToplevel(self)
        win.title(self.t("details_title"))
        win.geometry("640x380")
        win.configure(fg_color=pal["bg"])
        win.transient(self)
        exists = os.path.exists(item.path)
        changed = ""
        if exists and "empty_dir" not in item.categories:
            try:
                st = os.stat(item.path, follow_symlinks=False)
                if st.st_size != item.size or abs(st.st_mtime - item.mtime) > 1.0:
                    changed = self.t("details_changed")
            except OSError:
                changed = self.t("details_unreadable")
        rows = [
            (self.t("col_name"), item.name),
            (self.t("col_path"), item.path),
            (self.t("col_size"), f"{format_size(item.size)} ({item.size:,} B)"),
            (self.t("col_category"), ", ".join(self.t(f"reason_{c}") for c in item.categories)),
            (self.t("col_mtime"), format_mtime(item.mtime)),
            (self.t("col_drive"), item.drive),
            (self.t("details_group"), item.group_id or "-"),
            (self.t("col_status"), self.t("safety_primary_dup") if item.is_primary_in_group
             else self.t("safety_safe") if item.safety == SafetyLevel.SAFE.value else self.t("safety_candidate")),
            (self.t("details_exists"), (self.t("yes") if exists else self.t("no")) + (f" — {changed}" if changed else "")),
        ]
        card = Card(win, self)
        card.pack(fill="both", expand=True, padx=16, pady=16)
        for r, (k, v) in enumerate(rows):
            ctk.CTkLabel(card, text=k, font=self._font(12, "bold"), text_color=pal["subtext"], anchor="nw").grid(
                row=r, column=0, sticky="nw", padx=(16, 12), pady=4)
            ctk.CTkLabel(card, text=v, font=self._font(12), text_color=pal["text"], anchor="w", justify="left",
                         wraplength=440).grid(row=r, column=1, sticky="w", pady=4)
        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=(0, 16))
        self._button(btns, self.t("btn_open_folder"), lambda: open_in_explorer(item.path)).pack(side="left")
        self._button(btns, "OK", win.destroy, primary=True, width=90).pack(side="right")
        win.after(50, win.grab_set)

    # ------------------------------------------------------------------ cleanup

    def _build_cleanup(self, page: ctk.CTkFrame) -> None:
        pal = self.pal
        self._page_header(page, "cleanup_title", "cleanup_subtitle")

        summary = ctk.CTkFrame(page, fg_color="transparent")
        summary.pack(fill="x")
        self.cleanup_labels: dict[str, ctk.CTkLabel] = {}
        for col, key in enumerate(("cleanup_selected_count", "cleanup_safe_count",
                                   "cleanup_candidate_count", "cleanup_total_size")):
            summary.grid_columnconfigure(col, weight=1)
            card = Card(summary, self)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 12))
            ctk.CTkLabel(card, text=self.t(key), font=self._font(12), text_color=pal["subtext"], anchor="w",
                         wraplength=220, justify="left").pack(fill="x", padx=16, pady=(14, 0))
            lbl = ctk.CTkLabel(card, text="0", font=self._font(22, "bold"),
                               text_color=pal["cyan"] if col == 3 else pal["text"], anchor="w")
            lbl.pack(fill="x", padx=16, pady=(2, 14))
            self.cleanup_labels[key] = lbl

        preview_card = Card(page, self)
        preview_card.pack(fill="both", expand=True, pady=12)
        inner = tk.Frame(preview_card, bg=pal["row"], highlightthickness=0, bd=0)
        inner.pack(fill="both", expand=True, padx=6, pady=6)
        cols = ("action", "name", "path", "size", "category", "status")
        self.cleanup_tree = ttk.Treeview(inner, columns=cols, show="headings", style="CS.Treeview")
        headings = {
            "action": self.t("cleanup_col_action"), "name": self.t("col_name"), "path": self.t("col_path"),
            "size": self.t("col_size"), "category": self.t("col_category"), "status": self.t("col_status"),
        }
        widths = {"action": 150, "name": 220, "path": 420, "size": 100, "category": 170, "status": 170}
        for c in cols:
            self.cleanup_tree.heading(c, text=headings[c])
            self.cleanup_tree.column(c, width=widths[c], stretch=c in ("name", "path"), anchor="e" if c == "size" else "w")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=self.cleanup_tree.yview, style="CS.Vertical.TScrollbar")
        self.cleanup_tree.configure(yscrollcommand=vsb.set)
        self.cleanup_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.cleanup_tree.tag_configure("warn", foreground=pal["danger"])
        self.cleanup_tree.tag_configure("safe", foreground=pal["success"])

        self.cleanup_warning = ctk.CTkLabel(page, text="", font=self._font(12, "bold"), text_color=pal["danger"],
                                            anchor="w", justify="left", wraplength=1000)
        self.cleanup_warning.pack(fill="x")
        ctk.CTkLabel(page, text=self.t("cleanup_disclaimer"), font=self._font(12), text_color=pal["subtext"],
                     anchor="w", justify="left", wraplength=1000).pack(fill="x", pady=(4, 8))

        bottom = ctk.CTkFrame(page, fg_color="transparent")
        bottom.pack(fill="x")
        self.confirm_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            bottom, text=self.t("cleanup_confirm_check"), variable=self.confirm_var, font=self._font(12),
            text_color=pal["text"], fg_color=pal["accent"], hover_color=pal["accent_hover"],
            command=self._update_trash_button,
        ).pack(side="left")
        self.trash_btn = self._button(bottom, self.t("btn_trash"), self._do_cleanup, danger=True, width=200)
        self.trash_btn.pack(side="right")
        self._button(bottom, self.t("btn_deselect_all"), self.clear_selection).pack(side="right", padx=8)

    def _selected_items(self) -> list[ScanItem]:
        by_path = {i.path: i for i in self.items}
        return [by_path[p] for p in self.selected if p in by_path]

    def _fully_selected_groups(self, items: list[ScanItem]) -> set[str]:
        selected_paths = {i.path for i in items}
        groups: dict[str, list[ScanItem]] = {}
        for it in self.items:
            if it.group_id and "duplicate" in it.categories:
                groups.setdefault(it.group_id, []).append(it)
        return {
            gid for gid, members in groups.items()
            if len(members) >= 2 and all(m.path in selected_paths for m in members)
        }

    def _refresh_cleanup_page(self) -> None:
        if not hasattr(self, "cleanup_tree"):
            return
        items = sorted(self._selected_items(), key=lambda i: i.size, reverse=True)
        safe = [i for i in items if i.safety == SafetyLevel.SAFE.value and not i.is_primary_in_group]
        total = sum(i.size for i in items)
        self.cleanup_labels["cleanup_selected_count"].configure(text=f"{len(items):,}".replace(",", "."))
        self.cleanup_labels["cleanup_safe_count"].configure(text=f"{len(safe):,}".replace(",", "."))
        self.cleanup_labels["cleanup_candidate_count"].configure(text=f"{len(items) - len(safe):,}".replace(",", "."))
        self.cleanup_labels["cleanup_total_size"].configure(text=format_size(total))

        full_groups = self._fully_selected_groups(items)
        self.cleanup_warning.configure(
            text=self.t("cleanup_warn_full_group", count=len(full_groups)) if full_groups else ""
        )

        tree = self.cleanup_tree
        tree.delete(*tree.get_children())
        limit = 2000
        for n, it in enumerate(items[:limit]):
            in_full_group = bool(it.group_id and it.group_id in full_groups)
            is_safe = it.safety == SafetyLevel.SAFE.value and not it.is_primary_in_group
            tree.insert(
                "", "end", iid=f"c{n}",
                values=(
                    self.t("cleanup_action_trash"),
                    it.name,
                    os.path.dirname(it.path),
                    format_size(it.size) if "empty_dir" not in it.categories else "-",
                    ", ".join(self.t(f"reason_{c}") for c in it.categories),
                    self.t("safety_primary_dup") if it.is_primary_in_group
                    else self.t("safety_safe") if is_safe else self.t("safety_candidate"),
                ),
                tags=("warn",) if in_full_group or it.is_primary_in_group else ("safe",) if is_safe else (),
            )
        if len(items) > limit:
            tree.insert("", "end", iid="more", values=("", self.t("cleanup_more", count=len(items) - limit), "", "", "", ""))
        if not items:
            tree.insert("", "end", iid="empty", values=("", self.t("cleanup_empty_hint"), "", "", "", ""))
        self.confirm_var.set(False)
        self._update_trash_button()

    def _update_trash_button(self) -> None:
        if not hasattr(self, "trash_btn"):
            return
        enabled = (
            bool(self.selected)
            and self.confirm_var.get()
            and not self._cleanup_running
            and not self.controller.is_running
        )
        self.trash_btn.configure(state="normal" if enabled else "disabled")

    def _do_cleanup(self) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showwarning(self.t("confirm_title"), self.t("warn_Select_items"), parent=self)
            return
        safe_count = sum(1 for i in items if i.safety == SafetyLevel.SAFE.value and not i.is_primary_in_group)
        msg = self.t(
            "confirm_trash",
            count=len(items),
            size=format_size(sum(i.size for i in items)),
            safe_count=safe_count,
            candidate_count=len(items) - safe_count,
        )
        full_groups = self._fully_selected_groups(items)
        if full_groups:
            msg += "\n\n" + self.t("cleanup_warn_full_group", count=len(full_groups))
        if not messagebox.askyesno(self.t("confirm_title"), msg, icon="warning", parent=self):
            return
        self._start_cleanup(items)

    def duplicate_copies_to_remove(self) -> list[ScanItem]:
        """Every redundant copy of every verified duplicate group; one file per group is always kept.

        Groups whose primary copy is no longer present (on disk or in the results) are skipped,
        so a group can never lose all of its copies.
        """
        groups: dict[str, list[ScanItem]] = {}
        for it in self.items:
            if it.group_id and "duplicate" in it.categories:
                groups.setdefault(it.group_id, []).append(it)
        copies: list[ScanItem] = []
        for members in groups.values():
            primaries = [m for m in members if m.is_primary_in_group]
            if len(members) < 2 or len(primaries) != 1 or not os.path.exists(primaries[0].path):
                continue
            copies.extend(m for m in members if not m.is_primary_in_group)
        return copies

    def remove_all_duplicates(self) -> None:
        if self.controller.is_running or self._cleanup_running:
            return
        copies = self.duplicate_copies_to_remove()
        if not copies:
            messagebox.showinfo(self.t("confirm_title"), self.t("dup_remove_none"), parent=self)
            return
        groups = len({c.group_id for c in copies})
        msg = self.t(
            "dup_remove_confirm",
            count=len(copies),
            groups=groups,
            size=format_size(sum(c.size for c in copies)),
        )
        if not messagebox.askyesno(self.t("dup_remove_title"), msg, icon="warning", parent=self):
            return
        self._start_cleanup(copies)

    def _start_cleanup(self, items: list[ScanItem]) -> None:
        self._cleanup_running = True
        self._update_trash_button()
        self._set_status(self.t("status_cleanup_running", count=len(items)))
        exclusions = list(self.config_data.custom_exclusions)
        keepers = self._keepers_for(items)

        def _job() -> None:
            try:
                res = clean_items(items, custom_exclusions=exclusions, keepers=keepers)
            except Exception as exc:  # noqa: BLE001
                log.exception("Cleanup failed")
                res = CleanupResult(failed=[("-", str(exc))])
            self._bg_queue.put(("cleanup", res))

        threading.Thread(target=_job, name="cs-cleanup", daemon=True).start()

    def _on_cleanup_done(self, res: CleanupResult) -> None:
        self._cleanup_running = False
        trashed = set(res.trashed)
        changed = {p for p, _ in res.skipped_changed}
        self.items = [i for i in self.items if i.path not in trashed and i.path not in changed]
        self._drop_resolved_duplicate_groups()
        self._items_by_path = {normalize_win_path(i.path): i for i in self.items}
        self.selected -= trashed | changed
        lines = [self.t("result_ok", moved=len(res.trashed), size=format_size(res.bytes_sent))]
        if res.skipped_changed:
            lines.append(self.t("result_Skipped_modified", count=len(res.skipped_changed)))
        if res.failed:
            details = "\n".join(f"• {p}: {e}" for p, e in res.failed[:12])
            lines.append(self.t("result_errors", count=len(res.failed), details=details))
        messagebox.showinfo(self.t("result_title"), "\n\n".join(lines), parent=self)
        self._set_status(lines[0])
        self._refresh_tables()
        self._refresh_cleanup_page()
        self._load_drive_details_async()

    def _drop_resolved_duplicate_groups(self) -> None:
        """A group left with a single file is no longer a duplicate: strip that category from it."""
        sizes: dict[str, int] = {}
        for it in self.items:
            if it.group_id and "duplicate" in it.categories:
                sizes[it.group_id] = sizes.get(it.group_id, 0) + 1
        kept: list[ScanItem] = []
        for it in self.items:
            if it.group_id and "duplicate" in it.categories and sizes.get(it.group_id, 0) < 2:
                it.categories = tuple(c for c in it.categories if c != "duplicate")
                it.group_id = None
                it.is_primary_in_group = False
                if not it.categories:
                    self.selected.discard(it.path)
                    continue
                it.reason = it.categories[0]
            kept.append(it)
        self.items = kept

    # ------------------------------------------------------------------ history

    def _build_history(self, page: ctk.CTkFrame) -> None:
        pal = self.pal
        self._page_header(page, "history_title", "history_subtitle")
        card = Card(page, self)
        card.pack(fill="both", expand=True)
        inner = tk.Frame(card, bg=pal["row"], highlightthickness=0, bd=0)
        inner.pack(fill="both", expand=True, padx=6, pady=6)
        cols = ("date", "drive", "modes", "scanned", "found", "bytes", "elapsed", "status")
        self.history_tree = ttk.Treeview(inner, columns=cols, show="headings", style="CS.Treeview")
        for c in cols:
            key = "col_status" if c == "status" else f"history_col_{c}"
            self.history_tree.heading(c, text=self.t(key))
            self.history_tree.column(c, width=140 if c != "modes" else 320, stretch=c == "modes",
                                     anchor="e" if c in ("scanned", "found", "bytes", "elapsed") else "w")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=self.history_tree.yview, style="CS.Vertical.TScrollbar")
        self.history_tree.configure(yscrollcommand=vsb.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _refresh_history(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        tree = self.history_tree
        tree.delete(*tree.get_children())
        try:
            entries = list_scan_history(limit=200)
        except Exception as exc:  # noqa: BLE001
            log.warning("History load failed: %s", exc)
            entries = []
        for e in entries:
            tree.insert("", "end", iid=f"h{e.id}", values=(
                format_mtime(e.timestamp),
                ", ".join(d.rstrip("\\") for d in e.drives),
                ", ".join(self.t(f"mode_{m}") for m in e.modes),
                f"{e.files_seen:,}".replace(",", "."),
                f"{e.items_found:,}".replace(",", "."),
                format_size(e.potential_bytes),
                format_elapsed(e.duration_sec),
                self.t(f"state_{e.status.lower()}"),
            ))

    # ------------------------------------------------------------------ settings

    def _build_settings(self, page: ctk.CTkFrame) -> None:
        pal = self.pal
        cfg = self.config_data
        self._page_header(page, "settings_title", "settings_subtitle")
        scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        appearance = Card(scroll, self)
        appearance.pack(fill="x", pady=(0, 12))
        self._section_label(appearance, "settings_appearance")
        r = ctk.CTkFrame(appearance, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=(0, 14))
        self.lang_var = tk.StringVar(value="Italiano" if cfg.language == "it" else "English")
        ctk.CTkLabel(r, text=self.t("language"), text_color=pal["subtext"]).pack(side="left")
        ctk.CTkSegmentedButton(r, values=["Italiano", "English"], variable=self.lang_var,
                               selected_color=pal["accent"], selected_hover_color=pal["accent_hover"]).pack(side="left", padx=(8, 24))
        self.theme_var = tk.StringVar(value=self.t("theme_dark") if self.theme == "Dark" else self.t("theme_light"))
        ctk.CTkLabel(r, text=self.t("theme"), text_color=pal["subtext"]).pack(side="left")
        ctk.CTkSegmentedButton(r, values=[self.t("theme_dark"), self.t("theme_light")], variable=self.theme_var,
                               selected_color=pal["accent"], selected_hover_color=pal["accent_hover"]).pack(side="left", padx=8)

        thresholds = Card(scroll, self)
        thresholds.pack(fill="x", pady=(0, 12))
        self._section_label(thresholds, "settings_thresholds")
        self.set_vars: dict[str, tk.StringVar] = {
            "min_mb": tk.StringVar(value=f"{cfg.min_mb:g}"),
            "days_old": tk.StringVar(value=f"{cfg.days_old:g}"),
            "days_downloads": tk.StringVar(value=f"{cfg.days_downloads:g}"),
            "min_kb": tk.StringVar(value=f"{cfg.min_kb:g}"),
        }
        grid = ctk.CTkFrame(thresholds, fg_color="transparent")
        grid.pack(fill="x", padx=16, pady=(0, 14))
        for col, (key, label) in enumerate((("min_mb", "opt_min_mb"), ("days_old", "opt_days_old"),
                                            ("days_downloads", "opt_days_downloads"), ("min_kb", "opt_min_dup_kb"))):
            ctk.CTkLabel(grid, text=self.t(label), text_color=pal["subtext"]).grid(row=0, column=col * 2, padx=(0, 6), sticky="w")
            ctk.CTkEntry(grid, textvariable=self.set_vars[key], width=80).grid(row=0, column=col * 2 + 1, padx=(0, 18))

        perf = Card(scroll, self)
        perf.pack(fill="x", pady=(0, 12))
        self._section_label(perf, "settings_performance")
        self.use_cache_var = tk.BooleanVar(value=cfg.use_cache)
        ctk.CTkCheckBox(perf, text=self.t("settings_use_cache"), variable=self.use_cache_var, text_color=pal["text"],
                        fg_color=pal["accent"], hover_color=pal["accent_hover"]).pack(anchor="w", padx=16, pady=4)
        pr = ctk.CTkFrame(perf, fg_color="transparent")
        pr.pack(fill="x", padx=16, pady=4)
        self.workers_var = tk.StringVar(value=str(cfg.max_workers))
        self.page_size_var = tk.StringVar(value=str(cfg.page_size))
        ctk.CTkLabel(pr, text=self.t("settings_max_workers"), text_color=pal["subtext"]).pack(side="left")
        ctk.CTkEntry(pr, textvariable=self.workers_var, width=60).pack(side="left", padx=(6, 24))
        ctk.CTkLabel(pr, text=self.t("settings_page_size"), text_color=pal["subtext"]).pack(side="left")
        ctk.CTkEntry(pr, textvariable=self.page_size_var, width=70).pack(side="left", padx=6)
        cache_btns = ctk.CTkFrame(perf, fg_color="transparent")
        cache_btns.pack(fill="x", padx=16, pady=(6, 4))
        self._button(cache_btns, self.t("clear_all_cache"), self._clear_cache).pack(side="left")
        self._button(cache_btns, self.t("privacy_wipe"), self._wipe_local_data, danger=True).pack(
            side="left", padx=8)
        ctk.CTkLabel(
            perf, text=self.t("privacy_hint"), font=self._font(12), text_color=pal["subtext"],
            anchor="w", justify="left", wraplength=900,
        ).pack(fill="x", padx=16, pady=(0, 14))

        excl = Card(scroll, self)
        excl.pack(fill="x", pady=(0, 12))
        self._section_label(excl, "settings_exclusions")
        ctk.CTkLabel(excl, text=self.t("settings_exclusions_hint"), font=self._font(12), text_color=pal["subtext"],
                     anchor="w", justify="left", wraplength=900).pack(fill="x", padx=16)
        lb_frame = tk.Frame(excl, bg=pal["row"], highlightthickness=1, highlightbackground=pal["border"])
        lb_frame.pack(fill="x", padx=16, pady=8)
        self.excl_list = tk.Listbox(lb_frame, height=6, bg=pal["row"], fg=pal["text"], bd=0, highlightthickness=0,
                                    selectbackground=pal["select"], selectforeground=pal["text"], font=("Segoe UI", 10))
        self.excl_list.pack(fill="x", padx=4, pady=4)
        for p in cfg.custom_exclusions:
            self.excl_list.insert("end", p)
        er = ctk.CTkFrame(excl, fg_color="transparent")
        er.pack(fill="x", padx=16, pady=(0, 14))
        self._button(er, self.t("settings_add_exclusion"), self._add_exclusion).pack(side="left")
        self._button(er, self.t("settings_remove_exclusion"), self._remove_exclusion).pack(side="left", padx=8)

        self._button(scroll, self.t("settings_save"), self._save_settings, primary=True, width=200).pack(anchor="e", pady=8)

    def _add_exclusion(self) -> None:
        folder = filedialog.askdirectory(parent=self)
        if folder:
            folder = os.path.normpath(folder)
            if folder not in self.excl_list.get(0, "end"):
                self.excl_list.insert("end", folder)

    def _remove_exclusion(self) -> None:
        for idx in reversed(self.excl_list.curselection()):
            self.excl_list.delete(idx)

    def _clear_cache(self) -> None:
        if self.controller.is_running:
            return
        clear_drive_cache()
        self._set_status(self.t("cache_cleared"))

    def _wipe_local_data(self) -> None:
        if self.controller.is_running or self._cleanup_running:
            return
        if not messagebox.askyesno(self.t("privacy_wipe_title"), self.t("privacy_wipe_confirm"), parent=self):
            return
        erase_local_data()
        self._set_status(self.t("privacy_wiped"))

    def _keepers_for(self, items: list[ScanItem]) -> dict[str, tuple[str, ...]]:
        deleting = {i.path for i in items}
        keepers: dict[str, list[str]] = {}
        for it in self.items:
            if it.group_id and "duplicate" in it.categories and it.path not in deleting:
                keepers.setdefault(it.group_id, []).append(it.path)
        return {gid: tuple(paths) for gid, paths in keepers.items()}

    def _save_settings(self) -> None:
        cfg = self.config_data
        old_lang, old_theme = cfg.language, cfg.theme
        cfg.language = "it" if self.lang_var.get() == "Italiano" else "en"
        cfg.theme = "Dark" if self.theme_var.get() == self.t("theme_dark") else "Light"
        cfg.min_mb = _parse_float(self.set_vars["min_mb"].get(), cfg.min_mb)
        cfg.days_old = _parse_float(self.set_vars["days_old"].get(), cfg.days_old)
        cfg.days_downloads = _parse_float(self.set_vars["days_downloads"].get(), cfg.days_downloads)
        cfg.min_kb = _parse_float(self.set_vars["min_kb"].get(), cfg.min_kb)
        cfg.use_cache = bool(self.use_cache_var.get())
        cfg.max_workers = bounded_workers(_parse_float(self.workers_var.get(), cfg.max_workers))
        cfg.page_size = int(max(50, min(1000, _parse_float(self.page_size_var.get(), cfg.page_size))))
        cfg.custom_exclusions = [str(p) for p in self.excl_list.get(0, "end")]
        save_config(cfg)
        self.min_mb_var.set(f"{cfg.min_mb:g}")
        self.days_old_var.set(f"{cfg.days_old:g}")
        self.days_dl_var.set(f"{cfg.days_downloads:g}")
        self.min_kb_var.set(f"{cfg.min_kb:g}")
        if cfg.language != old_lang or cfg.theme != old_theme:
            set_language(cfg.language)
            ctk.set_appearance_mode(cfg.theme)
            self._build_ui()
        else:
            self._refresh_tables()
        self._set_status(self.t("settings_saved_msg"))

    # ------------------------------------------------------------------ event loop

    def _set_status(self, text: str) -> None:
        if hasattr(self, "status_bar"):
            self.status_bar.configure(text=text)

    def _apply_state(self, state: ScanState) -> None:
        if not hasattr(self, "scan_btn"):
            return
        running = state in (ScanState.SCANNING, ScanState.PAUSED)
        self.scan_btn.configure(state="disabled" if running else "normal")
        self.reapply_btn.configure(state="disabled" if running else "normal")
        self.pause_btn.configure(
            state="normal" if running else "disabled",
            text=self.t("btn_resume") if state == ScanState.PAUSED else self.t("btn_pause"),
        )
        self.cancel_btn.configure(state="normal" if running else "disabled")
        self.stat_labels["stat_state"].configure(text=self.t(f"state_{state.value.lower()}"))
        self.side_state.configure(text=f"● {self.t(f'state_{state.value.lower()}')}")
        if state == ScanState.SCANNING:
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.set(1.0 if state == ScanState.COMPLETED else 0.0)
        if state == ScanState.PAUSED:
            self._set_status(self.t("status_paused"))
        active = self._active_modes() if running else ()
        for mode, ctrl in self.category_controls.items():
            ctrl["scan"].configure(state="disabled" if running else "normal")
            ctrl["pause"].configure(
                state="normal" if running else "disabled",
                text=self.t("btn_resume") if state == ScanState.PAUSED else self.t("btn_pause"),
            )
            ctrl["cancel"].configure(state="normal" if running else "disabled")
            if state == ScanState.SCANNING and mode in active:
                ctrl["bar"].start()
            else:
                ctrl["bar"].stop()
                if not running:
                    ctrl["bar"].set(1.0 if state == ScanState.COMPLETED and mode in self._finished_modes else 0.0)
        self._update_trash_button()

    def _render_progress(self, prog: ScanProgress) -> None:
        fmt = lambda n: f"{n:,}".replace(",", ".")  # noqa: E731
        self.stat_labels["stat_files_seen"].configure(text=fmt(prog.files_seen))
        self.stat_labels["stat_results_found"].configure(text=fmt(max(prog.items_found, len(self.items))))
        self.stat_labels["stat_current_drive"].configure(text=(prog.drive or "-").rstrip("\\"))
        path = prog.current_path
        if len(path) > 140:
            path = path[:60] + " … " + path[-75:]
        self.path_label.configure(text=path)
        cat_text = self.t("cat_status", seen=fmt(prog.files_seen), drive=(prog.drive or "-").rstrip("\\"))
        for mode in self._active_modes():
            ctrl = self.category_controls.get(mode)
            if ctrl is not None:
                ctrl["status"].configure(text=cat_text)
        if prog.from_cache:
            self._set_status(self.t("status_loaded_cache", drive=prog.drive, count=prog.items_found))
        else:
            self._set_status(self.t(
                "status_scanning", drive=prog.drive, seen=fmt(prog.files_seen),
                found=fmt(prog.items_found), speed=fmt(int(prog.speed)), path=path,
            ))

    def _handle_event(self, ev: ControllerEvent) -> None:
        if ev.kind == "progress" and ev.progress is not None:
            self._last_progress = ev.progress
        elif ev.kind == "drive_done" and ev.drive_result is not None:
            self._add_items(ev.drive_result.items)
        elif ev.kind == "state":
            self._apply_state(ev.state)
        elif ev.kind == "finished":
            self._last_progress = None  # a progress event from the same poll batch must not overwrite the summary
            finished = self.controller.active_params
            self._finished_modes = finished.modes if finished is not None else ()
            self._apply_state(ev.state)
            self._refresh_tables()
            for mode in self._finished_modes:
                ctrl = self.category_controls.get(mode)
                if ctrl is None:
                    continue
                cat = MODE_TO_CATEGORY[mode]
                found = [i for i in self.items if cat in i.categories]
                key = "cat_cancelled" if ev.state == ScanState.CANCELLED else "cat_done"
                ctrl["status"].configure(text=self.t(
                    key, count=f"{len(found):,}".replace(",", "."),
                    size=format_size(sum(i.size for i in found)),
                ))
            elapsed = format_elapsed(self.controller.elapsed_seconds())
            if ev.state == ScanState.CANCELLED:
                self._set_status(self.t("status_cancelled", count=len(self.items)))
            else:
                self._set_status(self.t(
                    "status_done", count=len(self.items),
                    size=format_size(sum(i.size for i in self.items)), elapsed=elapsed,
                ))
            self.stat_labels["stat_results_found"].configure(text=f"{len(self.items):,}".replace(",", "."))
            self._on_params_changed()
            if self._current_page == "dashboard":
                self._refresh_dashboard()
        elif ev.kind == "error":
            self._apply_state(ScanState.FAILED)
            self._set_status(f"{self.t('state_failed')}: {ev.error}")
            messagebox.showerror("ClearSpace", ev.error, parent=self)

    def _poll(self) -> None:
        try:
            self._last_progress = None
            for ev in self.controller.poll_events(max_events=500):
                self._handle_event(ev)
            if self._last_progress is not None:
                self._render_progress(self._last_progress)
            if self.controller.is_running:
                self.stat_labels["stat_elapsed"].configure(text=format_elapsed(self.controller.elapsed_seconds()))
            while True:
                try:
                    kind, payload = self._bg_queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "drives":
                    self._drive_infos = {d.root: d for d in payload}
                    for root in self._drive_infos:
                        self.drive_vars.setdefault(root, tk.BooleanVar(value=False))
                    if hasattr(self, "drives_box"):
                        self._render_drive_checks()
                    self._refresh_dashboard()
                elif kind == "cleanup":
                    self._on_cleanup_done(payload)
        except Exception:  # noqa: BLE001
            log.exception("UI poll error")
        finally:
            self.after(POLL_MS, self._poll)

    def _on_close(self) -> None:
        if self.controller.is_running:
            self.controller.cancel()
            self.controller.wait_for_completion(timeout=3.0)
        self.destroy()


def run_app() -> None:
    setup_logging()

    def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
        log.error("Unhandled error in thread %s", getattr(args.thread, "name", "?"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = _log_thread_exception
    app = MainWindow()

    def _report_callback_exception(exc: type[BaseException], val: BaseException, tb: Any) -> None:
        # Tk callbacks must never take the whole application down (pythonw has no console).
        log.error("Unhandled UI error", exc_info=(exc, val, tb))
        try:
            messagebox.showerror("ClearSpace", f"{exc.__name__}: {val}", parent=app)
        except tk.TclError:
            pass

    app.report_callback_exception = _report_callback_exception  # type: ignore[method-assign]
    app.mainloop()
