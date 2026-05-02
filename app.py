"""Application coordinator — bridges GUI, Stats API watcher, and uploader."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from core.config import Config
from core.epic_auth import EpicAuthError, EpicClient
from core.stats_watcher import StatsWatcher
from core.uploader import BallchasingClient, UploadResult

logger = logging.getLogger(__name__)

_POST_GAME_DELAY_DEFAULT = 30.0  # fallback if config not loaded yet


class Application:
    def __init__(self, root) -> None:
        import tkinter as tk
        self.root: tk.Tk = root
        self.config = Config()
        self._watcher = StatsWatcher(port=self.config.stats_api_port)
        self._epic = EpicClient()
        self._uploaded_guids: set[str] = self.config.load_uploaded_guids()
        self._uploading_lock = threading.Lock()
        self._win: Optional["MainWindow"] = None  # set after GUI is built
        self._tray = None

        # Prevent double-upload if two end events fire close together
        self._last_game_end: float = 0.0

    # ── startup / teardown ────────────────────────────────────────────────────

    def start(self) -> None:
        from gui.main_window import MainWindow
        self._win = MainWindow(self.root, self)

        self._start_tray()  # must run first so self._tray is set

        # Only hide to tray if the tray actually started
        if self.config.start_minimized and self._tray is not None:
            self.root.withdraw()

        self._watcher.start()
        self._start_event_pump()
        self._verify_bc_token_async()

    def quit(self) -> None:
        self._watcher.stop()
        if self._tray:
            self._tray.stop()
        self.root.after(0, self.root.destroy)

    # ── public API (called by GUI) ────────────────────────────────────────────

    def trigger_manual_upload(self) -> None:
        """Immediately fetch latest unuploaded match from Epic and upload."""
        if not self.config.has_epic_auth:
            self.root.after(0, self._win.set_statusbar,
                            "Not logged in to Epic — open ⚙ Settings to connect.")
            return
        if not self.config.has_bc_token:
            self.root.after(0, self._win.set_statusbar,
                            "No Ballchasing token — open ⚙ Settings.")
            return
        threading.Thread(
            target=self._run_epic_upload, args=(0.0,), daemon=True, name="manual-upload"
        ).start()

    def on_settings_changed(self) -> None:
        self._watcher.stop()
        self._watcher = StatsWatcher(port=self.config.stats_api_port)
        self._watcher.start()
        self._verify_bc_token_async()
        self._refresh_epic_status_ui()

    def _refresh_epic_status_ui(self) -> None:
        cfg = self.config
        if cfg.has_epic_auth:
            name = cfg.epic_display_name.strip()
            self.root.after(0, self._win.set_epic_status, True,
                            f"Connected as {name}" if name else "Connected")
        else:
            self.root.after(0, self._win.set_epic_status, False, "Not connected — open ⚙ Settings")

    # ── event pump ───────────────────────────────────────────────────────────

    def _start_event_pump(self) -> None:
        self.root.after(100, self._pump)

    def _pump(self) -> None:
        q = self._watcher.event_queue
        while not q.empty():
            self._handle_watcher_event(q.get_nowait())
        self.root.after(100, self._pump)

    def _handle_watcher_event(self, msg: dict) -> None:
        t = msg.get("type", "")
        if t == "connected":
            self._win.set_rl_status(True, "Connected — watching for games")
            self._win.set_statusbar("Connected to Rocket League Stats API. Watching for games…")
        elif t == "disconnected":
            self._win.set_rl_status(None, "Waiting for Rocket League…")
            self._win.set_statusbar("Rocket League not running — waiting…")
        elif t == "connecting":
            self._win.set_rl_status(None, "Waiting for Rocket League…")
        elif t == "game_ended":
            now = time.monotonic()
            if now - self._last_game_end < 60:
                return  # debounce — ignore if already triggered within 60 s
            self._last_game_end = now
            self._win.set_statusbar("Game ended — fetching replay…")
            if self.config.auto_upload:
                delay = float(getattr(self.config, "post_game_delay", _POST_GAME_DELAY_DEFAULT))
                threading.Thread(
                    target=self._run_epic_upload,
                    args=(delay,),
                    daemon=True,
                    name="auto-upload",
                ).start()

    # ── Epic upload ───────────────────────────────────────────────────────────

    def _run_epic_upload(self, delay: float) -> None:
        """Wait `delay` seconds, then fetch + download + upload via Epic API."""
        if delay > 0:
            logger.info("Waiting %.0f seconds before fetching replay…", delay)
            time.sleep(delay)

        with self._uploading_lock:
            if not self.config.has_bc_token:
                logger.warning("Upload skipped — no Ballchasing token configured")
                self.root.after(0, self._win.set_statusbar,
                                "No Ballchasing token — open ⚙ Settings.")
                return
            if not self.config.has_epic_auth:
                logger.warning("Upload skipped — no Epic auth token")
                self.root.after(0, self._win.set_statusbar,
                                "Not logged in to Epic — open ⚙ Settings to connect.")
                return

            self.root.after(0, self._win.set_statusbar, "Fetching match from Epic API…")

            # Refresh EGS access token
            logger.info("Refreshing Epic Games access token for %s",
                        self.config.epic_display_name or self.config.epic_account_id)
            try:
                token_data = self._epic.refresh_login(self.config.epic_refresh_token)
                logger.info("Epic token refreshed — account: %s", token_data.get("display_name") or token_data.get("account_id"))
            except EpicAuthError as exc:
                logger.error("Epic token refresh failed: %s", exc)
                self.root.after(0, self._win.set_statusbar,
                                f"Epic login expired — re-authenticate in Settings. ({exc})")
                return
            except Exception as exc:
                logger.error("Network error refreshing Epic token: %s", exc)
                self.root.after(0, self._win.set_statusbar,
                                f"Network error — will retry next game: {exc}")
                return

            # Save refreshed tokens
            self.config.epic_refresh_token = token_data["refresh_token"]
            self.config.epic_account_id    = token_data["account_id"]
            self.config.epic_display_name  = token_data["display_name"]
            self.config.save()

            # Fetch latest unuploaded match
            batch_size = int(getattr(self.config, "upload_batch_size", 5))
            logger.info("Fetching match history from PsyNet for account %s (batch=%d)",
                        token_data["account_id"], batch_size)
            try:
                entries = self._epic.get_unuploaded_matches(
                    access_token  = token_data["access_token"],
                    account_id    = token_data["account_id"],
                    display_name  = token_data["display_name"],
                    uploaded_guids= self._uploaded_guids,
                    max_count     = batch_size,
                )
            except EpicAuthError as exc:
                logger.error("PsyNet match history request failed: %s", exc)
                self.root.after(0, self._win.set_statusbar,
                                f"Epic API error: {exc}")
                return
            except Exception as exc:
                logger.error("Network error fetching match history: %s", exc)
                self.root.after(0, self._win.set_statusbar,
                                f"Network error — will retry next game: {exc}")
                return

            if not entries:
                logger.info("No new unuploaded matches found in history")
                self.root.after(0, self._win.set_statusbar,
                                "No new matches found in Epic history — try Upload Now later.")
                return

            client = BallchasingClient(
                self.config.ballchasing_token,
                self.config.ballchasing_visibility,
            )

            for i, entry in enumerate(entries, 1):
                guid       = entry["match_guid"]
                replay_url = entry["replay_url"]
                filename   = f"{guid}.replay"

                self.root.after(0, self._win.set_statusbar,
                                f"Downloading replay {i}/{len(entries)}: {filename}…")
                logger.info("Downloading replay %s (%d/%d)", filename, i, len(entries))
                try:
                    data = self._epic.download_replay(replay_url)
                    logger.info("Replay downloaded — %d bytes", len(data))
                except EpicAuthError as exc:
                    logger.error("Replay download failed: %s", exc)
                    self.root.after(0, self._win.set_statusbar, f"Download failed: {exc}")
                    return
                except Exception as exc:
                    logger.error("Network error downloading replay: %s", exc)
                    self.root.after(0, self._win.set_statusbar,
                                    f"Network error downloading replay: {exc}")
                    return

                logger.info("Uploading %s to ballchasing (visibility=%s)",
                            filename, self.config.ballchasing_visibility)
                self.root.after(0, self._win.set_statusbar,
                                f"Uploading {i}/{len(entries)}: {filename} to ballchasing…")
                result = client.upload_bytes(filename, data)

                if result.ok and not result.duplicate:
                    logger.info("Upload successful — %s — %s", filename, result.url)
                elif result.duplicate:
                    logger.info("Already on ballchasing (duplicate) — %s", filename)
                else:
                    logger.error("Ballchasing upload failed — %s — %s", filename, result.error)

                self._uploaded_guids.add(guid)
                self.config.add_uploaded_guid(guid)
                self.root.after(0, self._on_upload_done, result)

    def _on_upload_done(self, result: UploadResult) -> None:
        if result.ok and not result.duplicate:
            msg = f"✅ Uploaded {result.filename}"
        elif result.duplicate:
            msg = f"⏭ Already on ballchasing: {result.filename}"
        else:
            msg = f"❌ Upload failed: {result.error}"

        self._win.set_statusbar(msg)
        self._win.add_upload_row(
            result.filename,
            ok=result.ok,
            duplicate=result.duplicate,
            url=result.url,
            error=result.error,
        )

    # ── ballchasing auth check ────────────────────────────────────────────────

    def _verify_bc_token_async(self) -> None:
        if not self.config.has_bc_token:
            self.root.after(0, self._win.set_bc_status, False,
                            "No API token — open ⚙ Settings")
            return

        def _check():
            client = BallchasingClient(self.config.ballchasing_token)
            ok, name, color = client.verify_token()
            if ok:
                text = f"Authenticated as {name}" if name else "Authenticated"
                self.root.after(0, self._win.set_bc_status, True, text, color)
            else:
                self.root.after(0, self._win.set_bc_status, False, f"Token error: {name}")

        threading.Thread(target=_check, daemon=True, name="bc-verify").start()

    # ── system tray ───────────────────────────────────────────────────────────

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image

            import sys
            _base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
            icon_path = _base / "assets" / "icon.png"
            icon_img = Image.open(icon_path).resize((64, 64), Image.LANCZOS)

            menu = pystray.Menu(
                pystray.MenuItem("Open", self._tray_open, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Upload Now", lambda: self.trigger_manual_upload()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", lambda: self.root.after(0, self.quit)),
            )

            self._tray = pystray.Icon(
                "hudayUpload",
                icon=icon_img,
                title="hudayUpload",
                menu=menu,
            )
            threading.Thread(
                target=self._tray.run, daemon=True, name="tray"
            ).start()
        except ImportError:
            logger.info("pystray/Pillow not installed — system tray disabled")

    def _tray_open(self) -> None:
        self.root.after(0, self._win.show)
