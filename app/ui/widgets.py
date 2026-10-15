from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable, Optional

import customtkinter as ctk

from app.core.disk import format_size
from app.core.models import SafetyLevel, ScanItem
from app.core.privacy import open_in_explorer

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow

CATEGORY_KEYS: tuple[str, ...] = (
    "large_file",
    "duplicate",
    "temp_cache",
    "old_file",
    "empty_dir",
    "old_download",
)

SORT_KEYS: tuple[str, ...] = (
    "sort_size_desc",
    "sort_size_asc",
    "sort_name_asc",
    "sort_name_desc",
    "sort_date_asc",
    "sort_date_desc",
)

COLUMNS: tuple[str, ...] = ("sel", "name", "path", "size", "category", "mtime", "drive", "status")

CHECK_ON = "\u2611"
CHECK_OFF = "\u2610"


def format_mtime(ts: float) -> str:
    if ts <= 0:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return "-"


class Card(ctk.CTkFrame):
    def __init__(self, master: tk.Misc, app: MainWindow, **kw: object) -> None:
        pal = app.pal
        super().__init__(
            master,
            fg_color=pal["card"],
            border_color=pal["border"],
            border_width=1,
            corner_radius=10,
            **kw,
        )


class ActivityBar(tk.Canvas):
    """Thin Fluent-style progress bar.

    CTkProgressBar re-renders its whole rounded shape on every animation frame
    (~10-20 ms per frame on the UI thread); this moves a single canvas item instead.
    """

    def __init__(self, master: tk.Misc, *, track: str, color: str, bg: str, height: int = 4) -> None:
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self._h = height
        self._track = self.create_rectangle(0, 0, 0, height, fill=track, width=0)
        self._bar = self.create_rectangle(0, 0, 0, height, fill=color, width=0)
        self._job: Optional[str] = None
        self._pos = 0.0
        self._value = 0.0
        self.bind("<Configure>", lambda _e: self._redraw())

    def _redraw(self) -> None:
        w = max(1, self.winfo_width())
        self.coords(self._track, 0, 0, w, self._h)
        if self._job is None:
            self.coords(self._bar, 0, 0, w * self._value, self._h)

    def set(self, value: float) -> None:
        self._value = max(0.0, min(1.0, value))
        self._redraw()

    def start(self) -> None:
        if self._job is None:
            self._tick()

    def stop(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        self._redraw()

    def _tick(self) -> None:
        w = max(1, self.winfo_width())
        seg = w * 0.22
        self._pos = (self._pos + w * 0.012) % (w + seg)
        self.coords(self._bar, self._pos - seg, 0, self._pos, self._h)
        self._job = self.after(33, self._tick)


class ResultsTable(ctk.CTkFrame):
    """Paginated view over the app's *valid filtered* results.

    Only one page of rows (``page_size``) exists as Treeview items at any time,
    so 100k+ results never create 100k+ widgets or rows.
    """

    def __init__(
        self,
        master: tk.Misc,
        app: MainWindow,
        *,
        fixed_category: Optional[str] = None,
        group_duplicates: bool = False,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.fixed_category = fixed_category
        self.group_duplicates = group_duplicates
        self._view: list[ScanItem] = []
        self._page = 0
        self._search_job: Optional[str] = None
        self._row_items: dict[str, ScanItem] = {}
        self._widget_state: dict[tuple[int, str], str] = {}

        t = app.t
        pal = app.pal

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", pady=(0, 8))

        self.search_var = tk.StringVar()
        # No textvariable: CTkEntry hides its placeholder when one is attached.
        self.search_entry = ctk.CTkEntry(
            toolbar,
            placeholder_text=t("search_placeholder"),
            height=34,
            corner_radius=8,
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.search_entry.bind("<KeyRelease>", lambda _e: self.search_var.set(self.search_entry.get()))
        self.search_var.trace_add("write", lambda *_: self._schedule_refresh())

        self._cat_labels: dict[str, str] = {t("filter_all_categories"): ""}
        for c in CATEGORY_KEYS:
            self._cat_labels[t(f"reason_{c}")] = c
        self.category_var = tk.StringVar(value=t("filter_all_categories"))
        if fixed_category is None:
            self.category_menu = ctk.CTkOptionMenu(
                toolbar,
                values=list(self._cat_labels.keys()),
                variable=self.category_var,
                command=lambda _v: self.refresh(reset_page=True),
                width=190,
                height=34,
                corner_radius=8,
                fg_color=pal["accent"],
                button_color=pal["accent_hover"],
            )
            self.category_menu.pack(side="left", padx=(0, 8))

        self._sort_labels = {t(k): k for k in SORT_KEYS}
        self.sort_var = tk.StringVar(value=t("sort_size_desc"))
        ctk.CTkOptionMenu(
            toolbar,
            values=list(self._sort_labels.keys()),
            variable=self.sort_var,
            command=lambda _v: self.refresh(reset_page=True),
            width=190,
            height=34,
            corner_radius=8,
            fg_color=pal["accent"],
            button_color=pal["accent_hover"],
        ).pack(side="left")

        table_card = Card(self, app)
        table_card.pack(fill="both", expand=True)
        inner = tk.Frame(table_card, bg=pal["row"], bd=0, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=6, pady=6)

        self.tree = ttk.Treeview(
            inner,
            columns=COLUMNS,
            show="headings",
            selectmode="extended",
            style="CS.Treeview",
        )
        widths = {
            "sel": 44,
            "name": 220,
            "path": 380,
            "size": 100,
            "category": 170,
            "mtime": 135,
            "drive": 60,
            "status": 170,
        }
        sortable = {"name": "name", "size": "size", "mtime": "date"}
        for col in COLUMNS:
            self.tree.heading(
                col,
                text=t(f"col_{'select' if col == 'sel' else col}"),
                command=(lambda c=col: self._on_heading(sortable[c])) if col in sortable else "",
            )
            self.tree.column(
                col,
                width=widths[col],
                minwidth=40,
                stretch=col in ("name", "path"),
                anchor="e" if col == "size" else ("center" if col in ("sel", "drive") else "w"),
            )
        vsb = ttk.Scrollbar(inner, orient="vertical", command=self.tree.yview, style="CS.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("odd", background=pal["row_alt"])
        self.tree.tag_configure("even", background=pal["row"])
        self.tree.tag_configure("safe", foreground=pal["success"])
        self.tree.tag_configure("primary", foreground=pal["cyan"])
        self.tree.tag_configure("checked", foreground=pal["accent"] if app.theme == "Light" else pal["text"])

        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<space>", lambda _e: self._toggle_selected_rows())
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Return>", lambda _e: self._details_focused())

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label=t("col_select"), command=self._toggle_selected_rows)
        self.menu.add_command(label=t("btn_open_folder"), command=self._open_focused)
        self.menu.add_command(label=t("btn_file_details"), command=self._details_focused)
        self.menu.add_command(label=t("ctx_copy_path"), command=self._copy_path)

        pager = ctk.CTkFrame(self, fg_color="transparent")
        pager.pack(fill="x", pady=(8, 0))
        self.prev_btn = ctk.CTkButton(
            pager, text=t("btn_prev_page"), width=90, height=30, corner_radius=8,
            fg_color=pal["card"], hover_color=pal["card_hover"], text_color=pal["text"],
            border_width=1, border_color=pal["border"], command=lambda: self._go_page(-1),
        )
        self.prev_btn.pack(side="left")
        self.page_label = ctk.CTkLabel(pager, text="", text_color=pal["subtext"])
        self.page_label.pack(side="left", padx=12)
        self.next_btn = ctk.CTkButton(
            pager, text=t("btn_next_page"), width=90, height=30, corner_radius=8,
            fg_color=pal["card"], hover_color=pal["card_hover"], text_color=pal["text"],
            border_width=1, border_color=pal["border"], command=lambda: self._go_page(1),
        )
        self.next_btn.pack(side="left")

        self.cleanup_btn = ctk.CTkButton(
            pager, text=t("btn_go_cleanup", count=0), height=32, corner_radius=8,
            fg_color=pal["accent"], hover_color=pal["accent_hover"],
            command=lambda: app.show_page("cleanup"),
        )
        self.cleanup_btn.pack(side="right")
        if fixed_category == "duplicate":
            self.remove_dups_btn = ctk.CTkButton(
                pager, text=t("btn_remove_all_dups"), height=32, corner_radius=8,
                fg_color=pal["danger_btn"], hover_color=pal["danger_btn_hover"], text_color="#FFFFFF",
                command=app.remove_all_duplicates,
            )
            self.remove_dups_btn.pack(side="right", padx=(0, 8))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", pady=(8, 0))
        self._action_btn(actions, t("btn_select_all"), self._select_page)
        if fixed_category in (None, "duplicate"):
            self._action_btn(actions, t("btn_select_dup_copies"), self._select_dup_copies)
        self._action_btn(actions, t("btn_select_safe"), self._select_safe)
        self._action_btn(actions, t("btn_deselect_all"), self._deselect_all)
        self._action_btn(actions, t("btn_open_folder"), self._open_focused)
        self._action_btn(actions, t("btn_file_details"), self._details_focused)

    def _action_btn(self, parent: tk.Misc, text: str, cmd: Callable[[], None]) -> None:
        pal = self.app.pal
        ctk.CTkButton(
            parent, text=text, height=30, corner_radius=8, command=cmd,
            fg_color=pal["card"], hover_color=pal["card_hover"], text_color=pal["text"],
            border_width=1, border_color=pal["border"],
        ).pack(side="left", padx=(0, 6))

    # ----- data pipeline (in-memory, valid results only) -----

    def _schedule_refresh(self) -> None:
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(180, lambda: self.refresh(reset_page=True))

    def _on_heading(self, field: str) -> None:
        t = self.app.t
        current = self._sort_labels.get(self.sort_var.get(), "sort_size_desc")
        if field == "size":
            nxt = "sort_size_asc" if current == "sort_size_desc" else "sort_size_desc"
        elif field == "name":
            nxt = "sort_name_desc" if current == "sort_name_asc" else "sort_name_asc"
        else:
            nxt = "sort_date_desc" if current == "sort_date_asc" else "sort_date_asc"
        self.sort_var.set(t(nxt))
        self.refresh(reset_page=True)

    def compute_view(self) -> list[ScanItem]:
        self._search_job = None
        category = self.fixed_category or self._cat_labels.get(self.category_var.get(), "")
        needle = self.search_var.get().strip().lower()
        items = self.app.items
        if category:
            items = [i for i in items if category in i.categories]
        if needle:
            cat_names = {c: self.app.t(f"reason_{c}").lower() for c in CATEGORY_KEYS}
            items = [
                i for i in items
                if needle in i.path.lower()
                or any(needle in cat_names.get(c, c) for c in i.categories)
            ]
        sort_key = self._sort_labels.get(self.sort_var.get(), "sort_size_desc")
        if sort_key.startswith("sort_size"):
            items = sorted(items, key=lambda i: i.size, reverse=sort_key.endswith("desc"))
        elif sort_key.startswith("sort_name"):
            items = sorted(items, key=lambda i: i.name.lower(), reverse=sort_key.endswith("desc"))
        else:
            items = sorted(items, key=lambda i: i.mtime, reverse=sort_key.endswith("desc"))
        if self.group_duplicates:
            # Keep each verified group contiguous, primary copy first; groups ordered by the chosen sort.
            order: dict[str, int] = {}
            for idx, it in enumerate(items):
                if it.group_id is not None and it.group_id not in order:
                    order[it.group_id] = idx
            items = sorted(
                items,
                key=lambda i: (order.get(i.group_id or "", 1 << 60), not i.is_primary_in_group, i.path.lower()),
            )
        return items

    def refresh(self, *, reset_page: bool = False) -> None:
        self._view = self.compute_view()
        if reset_page:
            self._page = 0
        self._render_page()

    def _page_count(self) -> int:
        size = self.app.page_size
        return max(1, (len(self._view) + size - 1) // size)

    def _go_page(self, delta: int) -> None:
        self._page = max(0, min(self._page_count() - 1, self._page + delta))
        self._render_page()

    def _status_text(self, item: ScanItem) -> str:
        t = self.app.t
        if item.is_primary_in_group:
            return t("safety_primary_dup")
        if item.safety == SafetyLevel.SAFE.value:
            return t("safety_safe")
        return t("safety_candidate")

    def _render_page(self) -> None:
        tree = self.tree
        tree.delete(*tree.get_children())
        self._row_items.clear()
        self._page = max(0, min(self._page, self._page_count() - 1))
        size = self.app.page_size
        start = self._page * size
        selected = self.app.selected
        t = self.app.t
        for n, item in enumerate(self._view[start : start + size]):
            iid = f"r{start + n}"
            self._row_items[iid] = item
            tags = ["odd" if n % 2 else "even"]
            if item.is_primary_in_group:
                tags.append("primary")
            elif item.safety == SafetyLevel.SAFE.value:
                tags.append("safe")
            category = ", ".join(t(f"reason_{c}") for c in item.categories)
            if item.group_id and "duplicate" in item.categories:
                category += f"  [{item.group_id[:6]}]"
            tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    CHECK_ON if item.path in selected else CHECK_OFF,
                    item.name,
                    os.path.dirname(item.path),
                    format_size(item.size) if "empty_dir" not in item.categories else "-",
                    category,
                    format_mtime(item.mtime),
                    item.drive.rstrip("\\") or "-",
                    self._status_text(item),
                ),
                tags=tags,
            )
        self._configure_if_changed(
            self.page_label,
            text=t(
                "page_info",
                page=self._page + 1,
                total_pages=self._page_count(),
                total_items=len(self._view),
            ),
        )
        self._configure_if_changed(self.prev_btn, state="normal" if self._page > 0 else "disabled")
        self._configure_if_changed(
            self.next_btn, state="normal" if self._page < self._page_count() - 1 else "disabled"
        )
        self.update_selection_label()

    def _configure_if_changed(self, widget: ctk.CTkBaseClass, **opts: str) -> None:
        # Every CTk configure() triggers a full canvas redraw, even for identical values.
        changed = {k: v for k, v in opts.items() if self._widget_state.get((id(widget), k)) != v}
        if changed:
            for k, v in changed.items():
                self._widget_state[(id(widget), k)] = v
            widget.configure(**changed)

    def update_selection_label(self) -> None:
        self._configure_if_changed(
            self.cleanup_btn, text=self.app.t("btn_go_cleanup", count=len(self.app.selected))
        )

    def refresh_checks(self) -> None:
        selected = self.app.selected
        for iid, item in self._row_items.items():
            self.tree.set(iid, "sel", CHECK_ON if item.path in selected else CHECK_OFF)
        self.update_selection_label()

    # ----- selection -----

    def _on_click(self, event: tk.Event) -> Optional[str]:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        iid = self.tree.identify_row(event.y)
        item = self._row_items.get(iid)
        if item is not None:
            self.app.toggle_selection([item])
        return "break"

    def _toggle_selected_rows(self) -> None:
        items = [self._row_items[i] for i in self.tree.selection() if i in self._row_items]
        if items:
            self.app.toggle_selection(items)

    def _select_page(self) -> None:
        # Bulk actions never pick the primary copy of a duplicate group.
        self.app.set_selected(
            [i for i in self._row_items.values() if not i.is_primary_in_group], True
        )

    def _select_safe(self) -> None:
        self.app.set_selected(
            [i for i in self._view if i.safety == SafetyLevel.SAFE.value and not i.is_primary_in_group],
            True,
        )

    def _select_dup_copies(self) -> None:
        self.app.set_selected(
            [
                i for i in self._view
                if "duplicate" in i.categories and i.group_id and not i.is_primary_in_group
            ],
            True,
        )

    def _deselect_all(self) -> None:
        self.app.clear_selection()

    # ----- row actions -----

    def _focused_item(self) -> Optional[ScanItem]:
        iid = self.tree.focus() or next(iter(self.tree.selection()), "")
        return self._row_items.get(iid)

    def _open_focused(self) -> None:
        item = self._focused_item()
        if item is not None:
            open_in_explorer(item.path)

    def _details_focused(self) -> None:
        item = self._focused_item()
        if item is not None:
            self.app.show_details(item)

    def _copy_path(self) -> None:
        item = self._focused_item()
        if item is not None:
            self.clipboard_clear()
            self.clipboard_append(item.path)

    def _on_double_click(self, event: tk.Event) -> None:
        if self.tree.identify_column(event.x) == "#1":
            return
        iid = self.tree.identify_row(event.y)
        item = self._row_items.get(iid)
        if item is not None:
            self.app.show_details(item)

    def _on_right_click(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
