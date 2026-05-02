"""Main application window — native-looking Windows GUI."""
from __future__ import annotations

import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Optional

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_APP_NAME = "hudayUpload"

if TYPE_CHECKING:
    from app import Application

# ── colour palette (matches Windows 10/11 light mode) ────────────────────────
_CLR_OK = "#107C10"       # Windows green
_CLR_WARN = "#CA5010"     # Windows orange
_CLR_ERR = "#D13438"      # Windows red
_CLR_NEUTRAL = "#767676"  # Windows grey
_CLR_ACCENT = "#0078D4"   # Windows blue


class MainWindow:
    def __init__(self, root: tk.Tk, app: "Application") -> None:
        self.root = root
        self.app = app

        root.title(_APP_NAME)
        root.geometry("480x540")
        root.minsize(380, 420)
        root.resizable(True, True)

        # Window icon
        try:
            from PIL import Image, ImageTk
            img = Image.open(_ASSETS_DIR / "icon.png")
            self._icon_photo = ImageTk.PhotoImage(img)
            root.iconphoto(True, self._icon_photo)
        except Exception:
            pass

        # Use the native Windows theme
        style = ttk.Style(root)
        _apply_theme(style)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        self._refresh_status()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = self.root

        # ── header bar ──────────────────────────────────────────────────────
        header = ttk.Frame(root, padding=(12, 10, 12, 6))
        header.pack(fill=tk.X)

        # Logo + app name
        try:
            from PIL import Image, ImageTk
            logo_img = Image.open(_ASSETS_DIR / "icon.png").resize((28, 28), Image.LANCZOS)
            self._logo_photo = ImageTk.PhotoImage(logo_img)
            ttk.Label(header, image=self._logo_photo).pack(side=tk.LEFT, padx=(0, 6))
        except Exception:
            pass

        ttk.Label(header, text=_APP_NAME, font=("Segoe UI", 13, "bold")).pack(
            side=tk.LEFT
        )

        self._btn_settings = ttk.Button(
            header, text="⚙ Settings", command=self._open_settings, width=12
        )
        self._btn_settings.pack(side=tk.RIGHT)

        # ── status section ───────────────────────────────────────────────────
        status_frame = ttk.LabelFrame(root, text="Status", padding=(12, 6, 12, 10))
        status_frame.pack(fill=tk.X, padx=12, pady=(0, 6))

        # Stats API row
        row1 = ttk.Frame(status_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Stats API", width=14, anchor="w").pack(side=tk.LEFT)
        self._dot_rl = _Dot(row1)
        self._dot_rl.pack(side=tk.LEFT, padx=(4, 6))
        self._lbl_rl = _ClickableLabel(row1, text="Connecting…", anchor="w")
        self._lbl_rl.pack(side=tk.LEFT, fill=tk.X)

        # Ballchasing row
        row2 = ttk.Frame(status_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Ballchasing", width=14, anchor="w").pack(side=tk.LEFT)
        self._dot_bc = _Dot(row2)
        self._dot_bc.pack(side=tk.LEFT, padx=(4, 6))
        self._lbl_bc = _ClickableLabel(row2, text="Not configured", anchor="w")
        self._lbl_bc.pack(side=tk.LEFT, fill=tk.X)

        # Epic Games row
        row3 = ttk.Frame(status_frame)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="Epic Games", width=14, anchor="w").pack(side=tk.LEFT)
        self._dot_epic = _Dot(row3)
        self._dot_epic.pack(side=tk.LEFT, padx=(4, 6))
        self._lbl_epic = _ClickableLabel(row3, text="Not connected", anchor="w")
        self._lbl_epic.pack(side=tk.LEFT, fill=tk.X)

        # ── setup banner (shown when critical items need configuring) ─────────
        self._banner = tk.Frame(root, bg="#FFF4CE", highlightbackground="#E8A000",
                                highlightthickness=1)
        self._banner_lbl = tk.Label(
            self._banner, bg="#FFF4CE", fg="#5D3A00",
            font=("Segoe UI", 9), anchor="w", padx=10, pady=6,
            text="", cursor="hand2",
        )
        self._banner_lbl.pack(fill=tk.X)
        self._banner_lbl.bind("<Button-1>", lambda _: self._open_settings())

        # ── activity log ─────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(root, text="Recent Uploads", padding=(8, 4, 8, 8))
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))

        cols = ("time", "status", "file")
        self._tree = ttk.Treeview(
            log_frame,
            columns=cols,
            show="headings",
            selectmode="browse",
            height=8,
        )
        self._tree.heading("time", text="Time")
        self._tree.heading("status", text="Result")
        self._tree.heading("file", text="Replay")
        self._tree.column("time", width=90, anchor="w", stretch=False)
        self._tree.column("status", width=80, anchor="center", stretch=False)
        self._tree.column("file", width=240, anchor="w", stretch=True)

        vsb = ttk.Scrollbar(log_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self._tree.tag_configure("ok", foreground=_CLR_OK)
        self._tree.tag_configure("dup", foreground=_CLR_NEUTRAL)
        self._tree.tag_configure("err", foreground=_CLR_ERR)

        self._tree.bind("<Double-1>", self._on_row_double_click)
        self._url_map: dict[str, str] = {}

        # ── bottom toolbar ────────────────────────────────────────────────────
        toolbar = ttk.Frame(root, padding=(12, 4, 12, 8))
        toolbar.pack(fill=tk.X)

        self._btn_upload = ttk.Button(
            toolbar, text="↑ Upload Now", command=self.app.trigger_manual_upload, width=14
        )
        self._btn_upload.pack(side=tk.LEFT, padx=(0, 6))

        ttk.Button(
            toolbar,
            text="🔗 Ballchasing",
            command=lambda: webbrowser.open("https://ballchasing.com"),
            width=14,
        ).pack(side=tk.LEFT)

        ttk.Button(
            toolbar, text="Minimize to Tray", command=self._minimize_to_tray, width=16
        ).pack(side=tk.RIGHT)

        # ── status bar ────────────────────────────────────────────────────────
        self._statusbar = ttk.Label(
            root, text="Initialising…", anchor="w", relief="sunken", padding=(6, 2)
        )
        self._statusbar.pack(fill=tk.X, side=tk.BOTTOM)

    # ── public update methods (called from app.py on main thread) ─────────────

    def set_rl_status(self, connected, text: str = "") -> None:
        """connected=True → green, False → orange (error), None → grey (idle wait)."""
        if connected is True:
            self._dot_rl.set_color(_CLR_OK)
            self._lbl_rl.set_normal(text or "Connected — watching for games")
        elif connected is None:
            self._dot_rl.set_color(_CLR_NEUTRAL)
            self._lbl_rl.set_normal(text or "Waiting for Rocket League…")
        else:
            self._dot_rl.set_color(_CLR_WARN)
            self._lbl_rl.set_normal(text or "Disconnected")
        self._update_banner()

    def set_bc_status(self, ok: bool, text: str = "", name_color: str = "") -> None:
        if ok:
            self._dot_bc.set_color(_CLR_OK)
            self._lbl_bc.set_normal(text or "Authenticated",
                                    color=name_color if name_color else _CLR_OK)
        else:
            self._dot_bc.set_color(_CLR_ERR)
            self._lbl_bc.set_action("⚙ No API token — click to open Settings",
                                    self._open_settings)
        self._update_banner()

    def set_epic_status(self, ok: bool, text: str = "") -> None:
        if ok:
            self._dot_epic.set_color(_CLR_OK)
            self._lbl_epic.set_normal(text or "Connected")
        else:
            self._dot_epic.set_color(_CLR_NEUTRAL)
            self._lbl_epic.set_action("⚙ Not connected — click to open Settings",
                                      self._open_settings)
        self._update_banner()

    def _update_banner(self) -> None:
        missing = []
        if not self.app.config.has_bc_token:
            missing.append("Ballchasing API token")
        if not self.app.config.has_epic_auth:
            missing.append("Epic Games account")
        if missing:
            self._banner_lbl.config(
                text="⚠  Setup required: " + " and ".join(missing)
                     + " — click here to open Settings"
            )
            self._banner.pack(fill=tk.X, padx=12, pady=(0, 4))
        else:
            self._banner.pack_forget()

    def set_statusbar(self, text: str) -> None:
        self._statusbar.config(text=text)

    def add_upload_row(
        self,
        filename: str,
        ok: bool,
        duplicate: bool,
        url: str = "",
        error: str = "",
    ) -> None:
        ts = time.strftime("%H:%M:%S")
        if ok and not duplicate:
            status = "✅ Uploaded"
            tag = "ok"
        elif duplicate:
            status = "⏭ Duplicate"
            tag = "dup"
        else:
            status = "❌ Failed"
            tag = "err"

        display_name = f"{filename}  →  view" if url else filename
        iid = self._tree.insert(
            "", 0, values=(ts, status, display_name), tags=(tag,)
        )
        if url:
            self._url_map[iid] = url
        self._tree.see(iid)

    # ── internals ────────────────────────────────────────────────────────────

    def _on_row_double_click(self, _event) -> None:
        sel = self._tree.selection()
        if sel and sel[0] in self._url_map:
            webbrowser.open(self._url_map[sel[0]])

    def _refresh_status(self) -> None:
        cfg = self.app.config
        if cfg.has_bc_token:
            self.set_statusbar("Watching for new replays…")
        else:
            self.set_bc_status(False)
            self.set_statusbar("Open ⚙ Settings to finish setup.")
        if cfg.has_epic_auth:
            name = cfg.epic_display_name.strip()
            self.set_epic_status(True, f"Connected as {name}" if name else "Connected")
        else:
            self.set_epic_status(False)

    def _open_settings(self) -> None:
        from gui.settings_dialog import SettingsDialog
        SettingsDialog(self.root, self.app)

    def _minimize_to_tray(self) -> None:
        self.root.withdraw()

    def _on_close(self) -> None:
        self._minimize_to_tray()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()


# ── helpers ───────────────────────────────────────────────────────────────────


class _ClickableLabel(ttk.Label):
    """A label that can switch between normal text and a clickable action link."""

    def __init__(self, parent, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self._action = None

    def set_normal(self, text: str, color: str = "") -> None:
        self.config(text=text, foreground=color or "", cursor="")
        self._unbind_click()
        self._action = None

    def set_action(self, text: str, callback) -> None:
        self.config(text=text, foreground=_CLR_ACCENT, cursor="hand2")
        self._action = callback
        self.bind("<Button-1>", lambda _: callback())

    def _unbind_click(self) -> None:
        try:
            self.unbind("<Button-1>")
        except Exception:
            pass


class _Dot(tk.Canvas):
    """Small coloured circle status indicator."""

    _SIZE = 12

    def __init__(self, parent, **kwargs) -> None:
        super().__init__(parent, width=self._SIZE, height=self._SIZE,
                         highlightthickness=0, **kwargs)
        self._oval = self.create_oval(1, 1, self._SIZE - 1, self._SIZE - 1,
                                      fill=_CLR_NEUTRAL, outline="")

    def set_color(self, color: str) -> None:
        self.itemconfig(self._oval, fill=color)


def _apply_theme(style: ttk.Style) -> None:
    available = style.theme_names()
    for name in ("vista", "winnative", "xpnative", "clam"):
        if name in available:
            style.theme_use(name)
            break

    style.configure("TLabel", font=("Segoe UI", 9))
    style.configure("TButton", font=("Segoe UI", 9))
    style.configure("TEntry", font=("Segoe UI", 9))
    style.configure("TLabelframe.Label", font=("Segoe UI", 9, "bold"))
    style.configure("Treeview", font=("Segoe UI", 9), rowheight=22)
    style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
