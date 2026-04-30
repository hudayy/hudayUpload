"""Application coordinator — bridges GUI, Stats API watcher, and uploader."""
from __future__ import annotations

import logging
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

from core.config import Config
from core.stats_watcher import StatsWatcher
from core.uploader import BallchasingClient, UploadResult, find_newest_replay, find_all_unuploaded

logger = logging.getLogger(__name__)

# How long to wait after ReplayCreated before looking for the file
_REPLAY_WRITE_DELAY = 4.0


class Application:
    def __init__(self, root) -> None:
        import tkinter as tk
        self.root: tk.Tk = root
        self.config = Config()
        self._watcher = StatsWatcher(port=self.config.stats_api_port)
        self._uploaded: set[str] = self.config.load_uploaded()
        self._uploading_lock = threading.Lock()
        self._win: Optional["MainWindow"] = None  # set after GUI is built
        self._tray = None

        # Guard: don't double-upload within 10 s of a ReplayCreated event
        self._last_replay_trigger: float = 0.0

    # ── startup / teardown ────────────────────────────────────────────────────

    def start(self) -> None:
        from gui.main_window import MainWindow
        self._win = MainWindow(self.root, self)

        self._start_tray()  # must run first so self._tray is set

        # Only hide to tray if the tray actually started; otherwise show normally
        if self.config.start_minimized and self._tray is not None:
            self.root.withdraw()

        self._watcher.start()
        self._start_event_pump()
        self._start_background_scan()
        self._verify_bc_token_async()

    def quit(self) -> None:
        self._watcher.stop()
        if self._tray:
            self._tray.stop()
        self.root.after(0, self.root.destroy)

    # ── public API (called by GUI) ────────────────────────────────────────────

    def trigger_manual_upload(self) -> None:
        threading.Thread(
            target=self._upload_newest, args=(None,), daemon=True, name="manual-upload"
        ).start()

    def on_settings_changed(self) -> None:
        # Restart watcher if port changed
        self._watcher.stop()
        self._watcher = StatsWatcher(port=self.config.stats_api_port)
        self._watcher.start()
        self._verify_bc_token_async()

    # ── event pump (polls watcher queue every 100 ms on main thread) ──────────

    def _start_event_pump(self) -> None:
        self.root.after(100, self._pump)

    def _pump(self) -> None:
        q = self._watcher.event_queue
        while not q.empty():
            msg = q.get_nowait()
            self._handle_watcher_event(msg)
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
            self._win.set_statusbar("Game ended — waiting for replay to save…")
            self._last_replay_trigger = time.monotonic()
            if self.config.auto_upload:
                threading.Thread(
                    target=self._upload_after_delay,
                    args=(msg.get("data", {}),),
                    daemon=True,
                    name="auto-upload",
                ).start()

    # ── upload logic ─────────────────────────────────────────────────────────

    def _upload_after_delay(self, event_data: dict) -> None:
        time.sleep(_REPLAY_WRITE_DELAY)
        self._upload_newest(event_data)

    def _upload_newest(self, event_data: Optional[dict]) -> None:
        with self._uploading_lock:
            if not self.config.has_bc_token:
                self.root.after(0, self._win.set_statusbar,
                                "No Ballchasing token — open ⚙ Settings.")
                return

            demos_dir = Path(self.config.replays_path)
            replay = find_newest_replay(demos_dir, self._uploaded, max_age_seconds=180)
            if replay is None:
                self.root.after(0, self._win.set_statusbar,
                                "No new replay found in the last 3 minutes.")
                return

            self.root.after(0, self._win.set_statusbar,
                            f"Uploading {replay.name}…")
            client = BallchasingClient(
                self.config.ballchasing_token,
                self.config.ballchasing_visibility,
            )
            result = client.upload(replay)
            self._uploaded.add(replay.name)
            self.config.add_uploaded(replay.name)
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

    # ── background scan (catches replays missed by Stats API) ─────────────────

    def _start_background_scan(self) -> None:
        """Scan every 5 minutes for replays that slipped past the event stream."""
        def _scan_loop():
            while True:
                time.sleep(300)
                try:
                    self._background_scan()
                except Exception as exc:
                    logger.debug("Background scan error: %s", exc)

        threading.Thread(target=_scan_loop, daemon=True, name="bg-scan").start()

    def _background_scan(self) -> None:
        if not self.config.auto_upload or not self.config.has_bc_token:
            return
        # Only run if we haven't just uploaded from an event (avoid double upload)
        if time.monotonic() - self._last_replay_trigger < 30:
            return
        demos_dir = Path(self.config.replays_path)
        missed = find_all_unuploaded(demos_dir, self._uploaded)
        for replay in missed[:5]:  # cap at 5 per cycle
            logger.info("Background scan: uploading missed replay %s", replay.name)
            with self._uploading_lock:
                client = BallchasingClient(
                    self.config.ballchasing_token,
                    self.config.ballchasing_visibility,
                )
                result = client.upload(replay)
                self._uploaded.add(replay.name)
                self.config.add_uploaded(replay.name)
                self.root.after(0, self._on_upload_done, result)

    # ── ballchasing auth check ────────────────────────────────────────────────

    def _verify_bc_token_async(self) -> None:
        if not self.config.has_bc_token:
            self.root.after(0, self._win.set_bc_status, False,
                            "No API token — open ⚙ Settings")
            return

        def _check():
            client = BallchasingClient(self.config.ballchasing_token)
            ok, info = client.verify_token()
            if ok:
                self.root.after(0, self._win.set_bc_status, True,
                                f"Authenticated as {info}" if info else "Authenticated")
            else:
                self.root.after(0, self._win.set_bc_status, False,
                                f"Token error: {info}")

        threading.Thread(target=_check, daemon=True, name="bc-verify").start()

    # ── system tray ───────────────────────────────────────────────────────────

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image

            icon_path = Path(__file__).resolve().parent / "assets" / "icon.png"
            icon_img = Image.open(icon_path).resize((64, 64), Image.LANCZOS)

            menu = pystray.Menu(
                pystray.MenuItem("Open", self._tray_open, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Upload Now", lambda: threading.Thread(
                    target=self._upload_newest, args=(None,), daemon=True
                ).start()),
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


