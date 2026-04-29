"""Settings dialog — modal window with native Windows look."""
from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app import Application


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, app: "Application") -> None:
        super().__init__(parent)
        self.app = app
        self.cfg = app.config

        self.title("Settings")
        self.resizable(False, False)
        self.grab_set()  # modal
        self.transient(parent)

        self._build()
        self._load_values()

        # Centre over parent
        self.update_idletasks()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        pad = {"padx": 14, "pady": 5}

        # ── Ballchasing section ──────────────────────────────────────────────
        bc_frame = ttk.LabelFrame(self, text="Ballchasing", padding=(10, 6, 10, 10))
        bc_frame.pack(fill=tk.X, **pad)

        ttk.Label(bc_frame, text="API Token").grid(row=0, column=0, sticky="w", pady=3)
        token_row = ttk.Frame(bc_frame)
        token_row.grid(row=0, column=1, sticky="ew", pady=3)
        bc_frame.columnconfigure(1, weight=1)

        self._token_var = tk.StringVar()
        self._token_entry = ttk.Entry(
            token_row, textvariable=self._token_var, show="•", width=32
        )
        self._token_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._show_token = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            token_row,
            text="Show",
            variable=self._show_token,
            command=self._toggle_token_visibility,
        ).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Button(
            bc_frame,
            text="Get API token ↗",
            command=lambda: webbrowser.open("https://ballchasing.com/upload"),
        ).grid(row=1, column=1, sticky="w", pady=(0, 2))

        self._bc_status_lbl = ttk.Label(bc_frame, text="", foreground="#107C10")
        self._bc_status_lbl.grid(row=2, column=0, columnspan=2, sticky="w")

        ttk.Button(bc_frame, text="Verify Token", command=self._verify_token).grid(
            row=2, column=1, sticky="e"
        )

        ttk.Label(bc_frame, text="Visibility").grid(row=3, column=0, sticky="w", pady=3)
        self._vis_var = tk.StringVar()
        ttk.Combobox(
            bc_frame,
            textvariable=self._vis_var,
            values=["public", "unlisted", "private"],
            state="readonly",
            width=12,
        ).grid(row=3, column=1, sticky="w", pady=3)

        # ── Rocket League section ────────────────────────────────────────────
        rl_frame = ttk.LabelFrame(
            self, text="Rocket League", padding=(10, 6, 10, 10)
        )
        rl_frame.pack(fill=tk.X, **pad)
        rl_frame.columnconfigure(1, weight=1)

        ttk.Label(rl_frame, text="Install Path").grid(row=0, column=0, sticky="w", pady=3)
        self._rl_path_var = tk.StringVar()
        ttk.Entry(rl_frame, textvariable=self._rl_path_var, width=36).grid(
            row=0, column=1, sticky="ew", pady=3
        )
        ttk.Button(rl_frame, text="Browse…", command=self._browse_rl).grid(
            row=0, column=2, padx=(4, 0), pady=3
        )

        ttk.Label(rl_frame, text="Replays Folder").grid(row=1, column=0, sticky="w", pady=3)
        self._replays_var = tk.StringVar()
        ttk.Entry(rl_frame, textvariable=self._replays_var, width=36).grid(
            row=1, column=1, sticky="ew", pady=3
        )
        ttk.Button(rl_frame, text="Browse…", command=self._browse_replays).grid(
            row=1, column=2, padx=(4, 0), pady=3
        )

        ttk.Label(rl_frame, text="Stats API Port").grid(row=2, column=0, sticky="w", pady=3)
        self._port_var = tk.StringVar()
        ttk.Entry(rl_frame, textvariable=self._port_var, width=8).grid(
            row=2, column=1, sticky="w", pady=3
        )

        # Derived ini path (read-only info, updates as replays folder changes)
        self._ini_derived_lbl = ttk.Label(
            rl_frame,
            text="Stats API ini: (set Replays Folder first)",
            foreground="#767676",
            wraplength=400,
        )
        self._ini_derived_lbl.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        self._ini_status_lbl = ttk.Label(rl_frame, text="", wraplength=400)
        self._ini_status_lbl.grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Button(
            rl_frame,
            text="Configure Stats API automatically",
            command=self._configure_stats_api,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ── Behaviour section ────────────────────────────────────────────────
        beh_frame = ttk.LabelFrame(self, text="Behaviour", padding=(10, 6, 10, 10))
        beh_frame.pack(fill=tk.X, **pad)

        self._auto_var = tk.BooleanVar()
        ttk.Checkbutton(
            beh_frame,
            text="Automatically upload replays when a game ends",
            variable=self._auto_var,
        ).pack(anchor="w")

        self._minimized_var = tk.BooleanVar()
        ttk.Checkbutton(
            beh_frame,
            text="Start minimized to system tray",
            variable=self._minimized_var,
        ).pack(anchor="w")

        self._startup_var = tk.BooleanVar()
        ttk.Checkbutton(
            beh_frame,
            text="Launch hudayUpload when Windows starts",
            variable=self._startup_var,
        ).pack(anchor="w")

        # ── buttons ──────────────────────────────────────────────────────────
        btn_row = ttk.Frame(self)
        btn_row.pack(fill=tk.X, padx=14, pady=(4, 12))

        ttk.Button(btn_row, text="Save", command=self._save, width=10).pack(
            side=tk.RIGHT, padx=(4, 0)
        )
        ttk.Button(btn_row, text="Cancel", command=self.destroy, width=10).pack(
            side=tk.RIGHT
        )

    # ── load / save ──────────────────────────────────────────────────────────

    def _load_values(self) -> None:
        self._token_var.set(self.cfg.ballchasing_token)
        self._vis_var.set(self.cfg.ballchasing_visibility)
        self._rl_path_var.set(self.cfg.rl_install_path)
        self._replays_var.set(self.cfg.replays_path)
        self._port_var.set(str(self.cfg.stats_api_port))
        self._auto_var.set(self.cfg.auto_upload)
        self._minimized_var.set(self.cfg.start_minimized)
        self._startup_var.set(self.cfg.launch_at_startup)
        self._replays_var.trace_add("write", lambda *_: self._refresh_ini_status())
        self._rl_path_var.trace_add("write", lambda *_: self._refresh_ini_status())
        self._refresh_ini_status()

    def _save(self) -> None:
        port_str = self._port_var.get().strip()
        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Port", "Port must be a number between 1 and 65535.", parent=self)
            return

        self.cfg.ballchasing_token = self._token_var.get().strip()
        self.cfg.ballchasing_visibility = self._vis_var.get()
        self.cfg.rl_install_path = self._rl_path_var.get().strip()
        self.cfg.replays_path = self._replays_var.get().strip()
        self.cfg.stats_api_port = port
        self.cfg.auto_upload = self._auto_var.get()
        self.cfg.start_minimized = self._minimized_var.get()
        self.cfg.launch_at_startup = self._startup_var.get()
        self.cfg.save()
        _apply_startup(self.cfg.launch_at_startup)

        self.app.on_settings_changed()
        self.destroy()

    # ── actions ──────────────────────────────────────────────────────────────

    def _toggle_token_visibility(self) -> None:
        self._token_entry.config(show="" if self._show_token.get() else "•")

    def _browse_rl(self) -> None:
        d = filedialog.askdirectory(
            title="Select Rocket League Install Folder (must contain TAGame\\)",
            initialdir=self._rl_path_var.get() or "C:/",
            parent=self,
        )
        if d:
            self._rl_path_var.set(d)
            self._validate_rl_path(d)
            self._refresh_ini_status()

    def _validate_rl_path(self, path: str) -> None:
        from pathlib import Path
        p = Path(path)
        tag = p / "TAGame" / "Config"
        if not tag.exists():
            self._ini_status_lbl.config(
                text=(
                    f"⚠ TAGame\\Config\\ not found inside:\n{path}\n"
                    "This may be the wrong folder. Look for the folder that\n"
                    "contains TAGame\\ and select that."
                ),
                foreground="#CA5010",
            )

    def _browse_replays(self) -> None:
        d = filedialog.askdirectory(
            title="Select Replays (Demos) Folder",
            initialdir=self._replays_var.get() or "C:/",
            parent=self,
        )
        if d:
            self._replays_var.set(d)

    def _verify_token(self) -> None:
        token = self._token_var.get().strip()
        if not token:
            self._bc_status_lbl.config(text="Enter a token first.", foreground="#D13438")
            return
        self._bc_status_lbl.config(text="Verifying…", foreground="#767676")
        self.update()

        def _check():
            from core.uploader import BallchasingClient
            ok, info = BallchasingClient(token).verify_token()
            self.after(0, lambda: self._bc_status_lbl.config(
                text=f"✅ Valid — {info}" if ok else f"❌ Invalid: {info}",
                foreground="#107C10" if ok else "#D13438",
            ))

        threading.Thread(target=_check, daemon=True).start()

    def _configure_stats_api(self) -> None:
        from core.rl_setup import enable_stats_api, read_ini_text

        ini = self._current_ini_path()
        if ini is None:
            messagebox.showwarning(
                "Missing paths",
                "Set the Install Path (or Replays Folder) first.",
                parent=self,
            )
            return

        # Update the path label so the user can see exactly where we're writing
        self._ini_derived_lbl.config(text=f"Stats API ini → {ini}", foreground="#0078D4")

        try:
            enable_stats_api(ini)
            content = read_ini_text(ini)
            self._ini_status_lbl.config(
                text=f"✅ Written to:\n{ini}\n\n{content.strip()}\n\nRestart Rocket League to apply.",
                foreground="#107C10",
            )
        except PermissionError:
            self._ini_status_lbl.config(
                text=(
                    f"❌ Permission denied writing to:\n{ini}\n\n"
                    "Try running hudayUpload as Administrator, or edit the file manually:\n"
                    f"{ini}"
                ),
                foreground="#D13438",
            )
        except Exception as exc:
            self._ini_status_lbl.config(
                text=f"❌ Could not write to:\n{ini}\n\n{exc}",
                foreground="#D13438",
            )

    def _refresh_ini_status(self) -> None:
        from core.rl_setup import check_stats_api_enabled, read_ini_text
        ini = self._current_ini_path()
        if ini is None:
            self._ini_derived_lbl.config(
                text="Stats API ini: set Replays Folder first",
                foreground="#767676",
            )
            self._ini_status_lbl.config(text="", foreground="#767676")
            return

        self._ini_derived_lbl.config(
            text=f"Stats API ini → {ini}",
            foreground="#0078D4",
        )

        if check_stats_api_enabled(ini):
            content = read_ini_text(ini)
            self._ini_status_lbl.config(
                text=f"✅ Stats API enabled:\n{content.strip()}",
                foreground="#107C10",
            )
        else:
            self._ini_status_lbl.config(
                text="⚠ Stats API not enabled — click 'Configure Stats API automatically'.",
                foreground="#CA5010",
            )

    def _current_ini_path(self) -> Path | None:
        """Return the ini path, preferring the install dir over the docs dir."""
        install = self._rl_path_var.get().strip()
        if install:
            return Path(install) / "TAGame" / "Config" / "DefaultStatsAPI.ini"
        replays = self._replays_var.get().strip()
        if replays:
            return Path(replays).parent / "Config" / "DefaultStatsAPI.ini"
        return None


# ── startup registry helper ───────────────────────────────────────────────────

def _apply_startup(enable: bool) -> None:
    """Add or remove hudayUpload from the Windows startup registry key."""
    import sys
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "hudayUpload"

    # Build the command: if running as a frozen exe use the exe path,
    # otherwise use 'pythonw main.py' so no console window appears.
    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        cmd = f'"{pythonw}" "{main_py}"'

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enable:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
    except OSError:
        pass
