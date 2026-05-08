"""Settings dialog — tabbed layout."""
from __future__ import annotations

import threading
import time
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
        self.grab_set()
        self.transient(parent)

        self._build()
        self._load_values()

        # Dark title bar to match the app theme
        from gui.main_window import _set_dark_titlebar
        self.after(10, lambda: _set_dark_titlebar(self))

        self.update_idletasks()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        # ── notebook ────────────────────────────────────────────────────────
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=14, pady=(12, 0))

        tab_beh  = ttk.Frame(nb, padding=(12, 10, 12, 12))
        tab_bc   = ttk.Frame(nb, padding=(12, 10, 12, 12))
        tab_epic = ttk.Frame(nb, padding=(12, 10, 12, 12))
        tab_rl   = ttk.Frame(nb, padding=(12, 10, 12, 12))

        nb.add(tab_beh,  text="  Behaviour  ")
        nb.add(tab_bc,   text="  Ballchasing  ")
        nb.add(tab_epic, text="  Epic Games  ")
        nb.add(tab_rl,   text="  Rocket League  ")

        self._build_behaviour(tab_beh)
        self._build_ballchasing(tab_bc)
        self._build_epic(tab_epic)
        self._build_rocket_league(tab_rl)

        # ── bottom bar ───────────────────────────────────────────────────────
        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=14, pady=(6, 12))

        ttk.Button(bottom, text="Save",   command=self._save,    width=10).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(bottom, text="Cancel", command=self.destroy,  width=10).pack(side=tk.RIGHT)
        ttk.Button(bottom, text="Export Logs", command=self._export_logs, width=14).pack(side=tk.LEFT)
        ttk.Button(
            bottom, text="Privacy Policy",
            command=lambda: webbrowser.open("https://huday.net/privacy-policy.html"),
            width=14,
        ).pack(side=tk.LEFT, padx=(6, 0))
        self._update_btn = ttk.Button(
            bottom, text="Check for Updates",
            command=self._check_for_updates, width=18,
        )
        self._update_btn.pack(side=tk.LEFT, padx=(6, 0))

    # ── tab: Behaviour ────────────────────────────────────────────────────────

    def _build_behaviour(self, parent: ttk.Frame) -> None:
        self._auto_var = tk.BooleanVar()
        ttk.Checkbutton(
            parent,
            text="Automatically upload replays",
            variable=self._auto_var,
        ).pack(anchor="w")

        self._minimized_var = tk.BooleanVar()
        ttk.Checkbutton(
            parent,
            text="Start minimized to system tray",
            variable=self._minimized_var,
        ).pack(anchor="w", pady=(4, 0))

        self._startup_var = tk.BooleanVar()
        ttk.Checkbutton(
            parent,
            text="Launch hudayUpload when Windows starts",
            variable=self._startup_var,
        ).pack(anchor="w", pady=(4, 0))

        ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=(10, 8))

        delay_row = ttk.Frame(parent)
        delay_row.pack(anchor="w")
        ttk.Label(delay_row, text="Delay after game end before fetching replay:").pack(side=tk.LEFT)
        self._delay_var = tk.IntVar()
        ttk.Spinbox(delay_row, textvariable=self._delay_var, from_=0, to=120, increment=5, width=5).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(delay_row, text="seconds").pack(side=tk.LEFT)

        every_n_row = ttk.Frame(parent)
        every_n_row.pack(anchor="w", pady=(8, 0))
        ttk.Label(every_n_row, text="Upload after every").pack(side=tk.LEFT)
        self._every_n_var = tk.IntVar()
        ttk.Spinbox(every_n_row, textvariable=self._every_n_var, from_=1, to=50, increment=1, width=5).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(every_n_row, text="games  (also uploads when Rocket League closes)").pack(side=tk.LEFT)

        batch_row = ttk.Frame(parent)
        batch_row.pack(anchor="w", pady=(8, 0))
        ttk.Label(batch_row, text="Matches to upload per pass:").pack(side=tk.LEFT)
        self._batch_var = tk.IntVar()
        ttk.Spinbox(batch_row, textvariable=self._batch_var, from_=1, to=50, increment=1, width=5).pack(side=tk.LEFT, padx=(6, 0))

    # ── tab: Ballchasing ─────────────────────────────────────────────────────

    def _build_ballchasing(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="API Token").grid(row=0, column=0, sticky="w", pady=3)
        token_row = ttk.Frame(parent)
        token_row.grid(row=0, column=1, sticky="ew", pady=3)

        self._token_var = tk.StringVar()
        self._token_entry = ttk.Entry(token_row, textvariable=self._token_var, show="•", width=32)
        self._token_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._show_token = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            token_row, text="Show",
            variable=self._show_token,
            command=self._toggle_token_visibility,
        ).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Button(
            parent, text="Get API token ↗",
            command=lambda: webbrowser.open("https://ballchasing.com/upload"),
        ).grid(row=1, column=1, sticky="w", pady=(0, 2))

        self._bc_status_lbl = ttk.Label(parent, text="")
        self._bc_status_lbl.grid(row=2, column=0, columnspan=2, sticky="w")

        ttk.Button(parent, text="Verify Token", command=self._verify_token).grid(
            row=2, column=1, sticky="e"
        )

        ttk.Label(parent, text="Visibility").grid(row=3, column=0, sticky="w", pady=(10, 3))
        self._vis_var = tk.StringVar()
        ttk.Combobox(
            parent, textvariable=self._vis_var,
            values=["public", "unlisted", "private"],
            state="readonly", width=12,
        ).grid(row=3, column=1, sticky="w", pady=(10, 3))

        ttk.Separator(parent, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(12, 8)
        )

        self._rocky_var = tk.BooleanVar()
        rocky_cb = ttk.Checkbutton(
            parent,
            text="Also upload to Rocky",
            variable=self._rocky_var,
        )
        rocky_cb.grid(row=5, column=0, columnspan=2, sticky="w")

        ttk.Button(
            parent, text="What is Rocky? ↗",
            command=lambda: webbrowser.open("https://github.com/LEX0RE/rockpload"),
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Separator(parent, orient="horizontal").grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(12, 8)
        )

        self._ballcam_var = tk.BooleanVar()
        ttk.Checkbutton(
            parent,
            text="Also upload to BallCam.tv",
            variable=self._ballcam_var,
        ).grid(row=8, column=0, columnspan=2, sticky="w")

        ttk.Label(parent, text="BallCam Token").grid(row=9, column=0, sticky="w", pady=(6, 3))
        ballcam_token_row = ttk.Frame(parent)
        ballcam_token_row.grid(row=9, column=1, sticky="ew", pady=(6, 3))
        self._ballcam_token_var = tk.StringVar()
        self._ballcam_token_entry = ttk.Entry(
            ballcam_token_row, textvariable=self._ballcam_token_var, show="•", width=32
        )
        self._ballcam_token_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._show_ballcam_token = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            ballcam_token_row, text="Show",
            variable=self._show_ballcam_token,
            command=self._toggle_ballcam_token_visibility,
        ).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Button(
            parent, text="Get BallCam token ↗",
            command=lambda: webbrowser.open("https://ballcam.tv"),
        ).grid(row=10, column=1, sticky="w", pady=(0, 2))

        ttk.Label(parent, text="BallCam Visibility").grid(row=11, column=0, sticky="w", pady=(4, 3))
        self._ballcam_vis_var = tk.StringVar()
        ttk.Combobox(
            parent, textvariable=self._ballcam_vis_var,
            values=["public", "unlisted"],
            state="readonly", width=12,
        ).grid(row=11, column=1, sticky="w", pady=(4, 3))

    # ── tab: Epic Games ───────────────────────────────────────────────────────

    def _build_epic(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        self._epic_status_lbl = ttk.Label(parent, text="Not connected", foreground="#9E9E9E")
        self._epic_status_lbl.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Button(parent, text="Connect Epic Account", command=self._connect_epic).grid(
            row=1, column=0, sticky="w"
        )
        ttk.Button(parent, text="Disconnect", command=self._disconnect_epic).grid(
            row=1, column=1, sticky="w", padx=(6, 0)
        )

    # ── tab: Rocket League ────────────────────────────────────────────────────

    def _build_rocket_league(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="Install Path").grid(row=0, column=0, sticky="w", pady=3)
        self._rl_path_var = tk.StringVar()
        ttk.Entry(parent, textvariable=self._rl_path_var, width=36).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(parent, text="Browse…", command=self._browse_rl).grid(row=0, column=2, padx=(4, 0), pady=3)

        ttk.Label(parent, text="Replays Folder").grid(row=1, column=0, sticky="w", pady=3)
        self._replays_var = tk.StringVar()
        ttk.Entry(parent, textvariable=self._replays_var, width=36).grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Button(parent, text="Browse…", command=self._browse_replays).grid(row=1, column=2, padx=(4, 0), pady=3)

        ttk.Label(parent, text="Stats API Port").grid(row=2, column=0, sticky="w", pady=3)
        self._port_var = tk.StringVar()
        ttk.Entry(parent, textvariable=self._port_var, width=8).grid(row=2, column=1, sticky="w", pady=3)

        self._ini_derived_lbl = ttk.Label(
            parent, text="Stats API ini: (set Replays Folder first)",
            foreground="#9E9E9E", wraplength=400,
        )
        self._ini_derived_lbl.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self._ini_status_lbl = ttk.Label(parent, text="", wraplength=400)
        self._ini_status_lbl.grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Button(
            parent, text="Configure Stats API automatically",
            command=self._configure_stats_api,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

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
        self._delay_var.set(int(self.cfg.post_game_delay))
        self._batch_var.set(int(self.cfg.upload_batch_size))
        self._every_n_var.set(int(self.cfg.upload_every_n_games))
        self._rocky_var.set(bool(self.cfg.rocky_enabled))
        self._ballcam_var.set(bool(self.cfg.ballcam_enabled))
        self._ballcam_token_var.set(self.cfg.ballcam_token)
        self._ballcam_vis_var.set(self.cfg.ballcam_visibility)
        self._refresh_epic_status()
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

        self.cfg.ballchasing_token      = self._token_var.get().strip()
        self.cfg.ballchasing_visibility = self._vis_var.get()
        self.cfg.rl_install_path        = self._rl_path_var.get().strip()
        self.cfg.replays_path           = self._replays_var.get().strip()
        self.cfg.stats_api_port         = port
        self.cfg.auto_upload            = self._auto_var.get()
        self.cfg.start_minimized        = self._minimized_var.get()
        self.cfg.launch_at_startup      = self._startup_var.get()
        self.cfg.post_game_delay        = self._delay_var.get()
        self.cfg.upload_batch_size      = self._batch_var.get()
        self.cfg.upload_every_n_games   = self._every_n_var.get()
        self.cfg.rocky_enabled          = self._rocky_var.get()
        self.cfg.ballcam_enabled        = self._ballcam_var.get()
        self.cfg.ballcam_token          = self._ballcam_token_var.get().strip()
        self.cfg.ballcam_visibility     = self._ballcam_vis_var.get()
        self.cfg.save()
        _apply_startup(self.cfg.launch_at_startup)

        self.app.on_settings_changed()
        self.destroy()

    # ── actions ──────────────────────────────────────────────────────────────

    def _toggle_token_visibility(self) -> None:
        self._token_entry.config(show="" if self._show_token.get() else "•")

    def _toggle_ballcam_token_visibility(self) -> None:
        self._ballcam_token_entry.config(show="" if self._show_ballcam_token.get() else "•")

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
        p = Path(path)
        if not (p / "TAGame" / "Config").exists():
            self._ini_status_lbl.config(
                text=(
                    f"⚠ TAGame\\Config\\ not found inside:\n{path}\n"
                    "This may be the wrong folder."
                ),
                foreground="#FF9800",
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
            self._bc_status_lbl.config(text="Enter a token first.", foreground="#EF5350")
            return
        self._bc_status_lbl.config(text="Verifying…", foreground="#9E9E9E")
        self.update()

        def _check():
            from core.uploader import BallchasingClient
            ok, name, color = BallchasingClient(token).verify_token()
            if ok:
                text = f"✅ Valid — {name}" if name else "✅ Valid"
                self.after(0, lambda: self._bc_status_lbl.config(text=text, foreground=color))
            else:
                self.after(0, lambda: self._bc_status_lbl.config(
                    text=f"❌ Invalid: {name}", foreground="#EF5350"
                ))

        threading.Thread(target=_check, daemon=True).start()

    def _configure_stats_api(self) -> None:
        from core.rl_setup import enable_stats_api, read_ini_text

        ini = self._current_ini_path()
        if ini is None:
            messagebox.showwarning("Missing paths", "Set the Install Path (or Replays Folder) first.", parent=self)
            return

        self._ini_derived_lbl.config(text=f"Stats API ini → {ini}", foreground="#42A5F5")

        try:
            enable_stats_api(ini)
            content = read_ini_text(ini)
            self._ini_status_lbl.config(
                text=f"✅ Written to:\n{ini}\n\n{content.strip()}\n\nRestart Rocket League to apply.",
                foreground="#4CAF50",
            )
        except PermissionError:
            self._ini_status_lbl.config(
                text=(
                    f"❌ Permission denied writing to:\n{ini}\n\n"
                    "Try running hudayUpload as Administrator."
                ),
                foreground="#EF5350",
            )
        except Exception as exc:
            self._ini_status_lbl.config(
                text=f"❌ Could not write to:\n{ini}\n\n{exc}",
                foreground="#EF5350",
            )

    def _refresh_ini_status(self) -> None:
        from core.rl_setup import check_stats_api_enabled, read_ini_text
        ini = self._current_ini_path()
        if ini is None:
            self._ini_derived_lbl.config(text="Stats API ini: set Replays Folder first", foreground="#9E9E9E")
            self._ini_status_lbl.config(text="", foreground="#9E9E9E")
            return

        self._ini_derived_lbl.config(text=f"Stats API ini → {ini}", foreground="#42A5F5")

        if check_stats_api_enabled(ini):
            content = read_ini_text(ini)
            self._ini_status_lbl.config(
                text=f"✅ Stats API enabled:\n{content.strip()}",
                foreground="#4CAF50",
            )
        else:
            self._ini_status_lbl.config(
                text="⚠ Stats API not enabled — click 'Configure Stats API automatically'.",
                foreground="#FF9800",
            )

    def _refresh_epic_status(self) -> None:
        name = self.cfg.epic_display_name.strip()
        if self.cfg.has_epic_auth and name:
            self._epic_status_lbl.config(text=f"Connected as {name}", foreground="#4CAF50")
        elif self.cfg.has_epic_auth:
            self._epic_status_lbl.config(text="Connected (display name unknown)", foreground="#4CAF50")
        else:
            self._epic_status_lbl.config(text="Not connected", foreground="#9E9E9E")

    def _connect_epic(self) -> None:
        from core.epic_auth import EpicClient, EpicAuthError

        client = EpicClient()
        client.open_auth_browser()

        dlg = tk.Toplevel(self)
        dlg.title("Connect Epic Account")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self)

        ttk.Label(
            dlg,
            text=(
                "A page opened in your browser.\n\n"
                "Log in, then copy the Authorization Code shown on the page —\n"
                "this window will close automatically."
            ),
            justify="left",
            wraplength=320,
            padding=(14, 12, 14, 4),
        ).pack(fill=tk.X)

        status_lbl = ttk.Label(
            dlg, text="Watching clipboard for code…", foreground="#9E9E9E",
            padding=(14, 0, 14, 8),
        )
        status_lbl.pack(fill=tk.X)

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=14, pady=(0, 10))
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy, width=10).pack(side=tk.RIGHT)

        dlg.update_idletasks()
        px, py = self.winfo_x(), self.winfo_y()
        pw, ph = self.winfo_width(), self.winfo_height()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        dlg.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

        _cancelled = [False]
        _seen_clips: set = set()

        def _try_code(code: str) -> None:
            code = code.strip()
            if not code or code in _seen_clips:
                return
            _seen_clips.add(code)
            dlg.after(0, lambda: status_lbl.config(text="Code detected — connecting…", foreground="#9E9E9E"))

            def _do():
                try:
                    data = client.login_with_code(code)
                    def _ok():
                        self.cfg.epic_refresh_token = data["refresh_token"]
                        self.cfg.epic_account_id    = data["account_id"]
                        self.cfg.epic_display_name  = data["display_name"]
                        self.cfg.save()
                        self._refresh_epic_status()
                        self.app._refresh_epic_status_ui()
                        dlg.destroy()
                    dlg.after(0, _ok)
                except EpicAuthError as exc:
                    _seen_clips.discard(code)
                    msg = str(exc)
                    dlg.after(0, lambda m=msg: status_lbl.config(text=f"❌ {m}", foreground="#EF5350"))
                except Exception as exc:
                    _seen_clips.discard(code)
                    msg = str(exc)
                    dlg.after(0, lambda m=msg: status_lbl.config(text=f"❌ Unexpected error: {m}", foreground="#EF5350"))

            threading.Thread(target=_do, daemon=True).start()

        def _extract_code(clip: str) -> str:
            clip = clip.strip()
            if not clip:
                return ""
            if clip.startswith("{"):
                try:
                    import json as _json
                    obj = _json.loads(clip)
                    code = obj.get("authorizationCode") or obj.get("code") or ""
                    return code.strip()
                except Exception:
                    return ""
            if len(clip) <= 64 and clip.replace("-", "").isalnum():
                return clip
            return ""

        def _poll_clipboard():
            if _cancelled[0] or not dlg.winfo_exists():
                return
            try:
                clip = dlg.clipboard_get()
                code = _extract_code(clip)
                if code:
                    _try_code(code)
            except Exception:
                pass
            dlg.after(500, _poll_clipboard)

        def _on_close():
            _cancelled[0] = True
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", _on_close)
        dlg.after(500, _poll_clipboard)

    def _check_for_updates(self) -> None:
        self._update_btn.config(state="disabled", text="Checking…")

        def _on_result(info: dict | None, current_version: str) -> None:
            if not self.winfo_exists():
                return
            self._update_btn.config(state="normal", text="Check for Updates")
            if info:
                # The update banner is already shown by app.check_for_update_manual.
                messagebox.showinfo(
                    "Update Available",
                    f"Version {info['version']} is available. "
                    f"You're on {current_version}.\n\n"
                    "Click 'Update' on the banner in the main window to install.",
                    parent=self,
                )
            else:
                messagebox.showinfo(
                    "Up to Date",
                    f"You're running the latest version ({current_version}).",
                    parent=self,
                )

        self.app.check_for_update_manual(_on_result)

    def _export_logs(self) -> None:
        from core.log_buffer import log_buffer
        lines = list(log_buffer.records)
        if not lines:
            messagebox.showinfo("Export Logs", "No log entries yet.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Export Logs",
            defaultextension=".txt",
            initialfile=f"hudayUpload_{time.strftime('%Y%m%d_%H%M%S')}.log",
            filetypes=[("Text files", "*.txt *.log"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            messagebox.showinfo("Export Logs", f"Logs saved to:\n{path}", parent=self)
        except OSError as exc:
            messagebox.showerror("Export Failed", str(exc), parent=self)

    def _disconnect_epic(self) -> None:
        self.cfg.epic_refresh_token = ""
        self.cfg.epic_account_id    = ""
        self.cfg.epic_display_name  = ""
        self.cfg.save()
        self._refresh_epic_status()
        self.app._refresh_epic_status_ui()

    def _current_ini_path(self) -> Path | None:
        install = self._rl_path_var.get().strip()
        if install:
            return Path(install) / "TAGame" / "Config" / "DefaultStatsAPI.ini"
        replays = self._replays_var.get().strip()
        if replays:
            return Path(replays).parent / "Config" / "DefaultStatsAPI.ini"
        return None


# ── startup registry helper ───────────────────────────────────────────────────

def _apply_startup(enable: bool) -> None:
    import sys
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "hudayUpload"

    if getattr(sys, "frozen", False):
        cmd = f'"{sys.executable}"'
    else:
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        cmd = f'"{pythonw}" "{main_py}"'

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
    except OSError:
        pass
