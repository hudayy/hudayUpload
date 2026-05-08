"""Application coordinator — bridges GUI, Stats API watcher, and uploader."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from core.config import Config
from core.epic_auth import EpicAuthError, EpicClient
from core.replay_meta import build_title, parse_header, write_replay_name
from core.stats_watcher import StatsWatcher
from core.updater import check_for_update, download_and_install
from core.uploader import BallcamClient, BallchasingClient, RockyClient, UploadResult

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

        # Debounce: prevent MatchEnded + PodiumStart from counting as two games
        self._last_game_end: float = 0.0
        # Count of games played since the last upload batch
        self._games_since_upload: int = 0
        # Match state data from UpdateState events, keyed by MatchGuid
        self._match_states: dict[str, dict] = {}
        # Pause flag — when True the StatsWatcher is stopped and won't reconnect
        self._paused: bool = False

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
        self._check_for_update_async()
        self._write_stats_api_tickrate()

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
        self._games_since_upload = 0
        threading.Thread(
            target=self._run_epic_upload, args=(0.0,), daemon=True, name="manual-upload"
        ).start()

    def on_settings_changed(self) -> None:
        self._watcher.stop()
        self._watcher = StatsWatcher(port=self.config.stats_api_port)
        if not self._paused:
            self._watcher.start()
        self._verify_bc_token_async()
        self._refresh_epic_status_ui()

    def toggle_pause(self) -> None:
        """Pause or resume the Stats API connection to reduce in-game CPU load."""
        if self._paused:
            self._paused = False
            self._watcher.start()
            logger.info("Monitoring resumed")
            if self._win:
                self.root.after(0, self._win.set_paused_state, False)
        else:
            self._paused = True
            self._watcher.stop()
            logger.info("Monitoring paused by user")
            if self._win:
                self.root.after(0, self._win.set_paused_state, True)

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
            if self.config.auto_upload and self._games_since_upload > 0:
                logger.info("Rocket League closed — triggering upload for %d pending game(s)",
                            self._games_since_upload)
                self._win.set_statusbar(
                    f"Rocket League closed — uploading {self._games_since_upload} game(s)…"
                )
                self._games_since_upload = 0
                delay = float(getattr(self.config, "post_game_delay", _POST_GAME_DELAY_DEFAULT))
                threading.Thread(
                    target=self._run_epic_upload,
                    args=(delay,),
                    daemon=True,
                    name="auto-upload-on-close",
                ).start()
            else:
                self._win.set_statusbar("Rocket League not running — waiting…")
        elif t == "connecting":
            self._win.set_rl_status(None, "Waiting for Rocket League…")
        elif t == "match_state":
            guid = msg.get("match_guid", "")
            if guid:
                self._match_states[guid] = {
                    "players": msg.get("players", []),
                    "teams":   msg.get("teams", []),
                }
                logger.debug("Stored match_state for %s", guid)
        elif t == "game_ended":
            now = time.monotonic()
            if now - self._last_game_end < 60:
                return  # debounce — MatchEnded + PodiumStart fire within ~3 s of each other
            self._last_game_end = now
            self._games_since_upload += 1
            every_n = int(getattr(self.config, "upload_every_n_games", 15))
            logger.info("Game ended — %d/%d games since last upload",
                        self._games_since_upload, every_n)
            self._win.set_statusbar(
                f"Game {self._games_since_upload}/{every_n} — "
                + ("uploading when Rocket League closes or limit reached."
                   if self.config.auto_upload else "auto-upload disabled.")
            )
            if self.config.auto_upload and self._games_since_upload >= every_n:
                logger.info("Game limit reached (%d) — triggering upload", every_n)
                self._games_since_upload = 0
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

                props = parse_header(data)

                # Inject fields not present in the replay header.
                # Primary source: PsyNet _raw_entry Match object.
                raw_match = entry.get("_raw_entry", {}).get("Match", {})
                if not props.get("Playlist"):
                    props["Playlist"] = entry.get("playlist_id", 0)
                if not props.get("PlayerName"):
                    props["PlayerName"] = self._psynet_player_name(entry)
                if not isinstance(props.get("WinningTeam"), int):
                    wt = raw_match.get("WinningTeam")
                    if isinstance(wt, int):
                        props["WinningTeam"] = wt
                if not isinstance(props.get("Team0Score"), int):
                    s = raw_match.get("Team0Score")
                    if isinstance(s, int):
                        props["Team0Score"] = s
                if not isinstance(props.get("Team1Score"), int):
                    s = raw_match.get("Team1Score")
                    if isinstance(s, int):
                        props["Team1Score"] = s
                if not isinstance(props.get("PrimaryPlayerTeam"), int):
                    my_name = (self.config.epic_display_name or "").rstrip(".").strip().lower()
                    for p in raw_match.get("Players", []):
                        if (p.get("PlayerName") or "").strip().lower() == my_name:
                            props["PrimaryPlayerTeam"] = p.get("LastTeam", -1)
                            break

                # Supplement with Stats API UpdateState data if available
                state = self._match_states.get(guid)
                if state:
                    if not isinstance(props.get("PrimaryPlayerTeam"), int):
                        my_name = (self.config.epic_display_name or "").rstrip(".").strip().lower()
                        for p in state["players"]:
                            if p.get("name", "").strip().lower() == my_name or p.get("primary"):
                                props["PrimaryPlayerTeam"] = p["team"]
                                break
                    if not isinstance(props.get("WinningTeam"), int):
                        teams = state.get("teams", [])
                        if teams:
                            best = max(teams, key=lambda t: t.get("score", 0))
                            props["WinningTeam"] = best.get("team_num", 0)
                    for t in state.get("teams", []):
                        tn = t.get("team_num", -1)
                        if tn == 0 and not props.get("Team0Score"):
                            props["Team0Score"] = t.get("score", 0)
                        elif tn == 1 and not props.get("Team1Score"):
                            props["Team1Score"] = t.get("score", 0)

                title = build_title(props, fallback_name=self.config.epic_display_name or "")

                # Write the title into the replay's ReplayName binary property
                if title:
                    data = write_replay_name(data, title)

                # Use the title as the filename — ballchasing sets the replay's
                # display name from the uploaded filename, no API PATCH needed.
                upload_name = f"{title}.replay" if title else filename
                logger.info("Uploading %s as %r (visibility=%s)",
                            filename, upload_name, self.config.ballchasing_visibility)
                self.root.after(0, self._win.set_statusbar,
                                f"Uploading {i}/{len(entries)}: {upload_name} to ballchasing…")
                result = client.upload_bytes(upload_name, data)

                if result.ok and not result.duplicate:
                    logger.info("Upload successful — %s — %s", filename, result.url)
                elif result.duplicate:
                    logger.info("Already on ballchasing (duplicate) — %s", filename)
                else:
                    logger.error("Ballchasing upload failed — %s — %s", filename, result.error)

                # Optional Rocky upload
                if getattr(self.config, "rocky_enabled", False):
                    self.root.after(0, self._win.set_statusbar,
                                    f"Uploading {i}/{len(entries)}: {upload_name} to Rocky…")
                    rocky_result = RockyClient().upload_bytes(upload_name, data)
                    if rocky_result.ok and not rocky_result.duplicate:
                        logger.info("Rocky upload successful — %s", filename)
                    elif rocky_result.duplicate:
                        logger.info("Rocky: already uploaded (duplicate) — %s", filename)
                    else:
                        logger.error("Rocky upload failed — %s — %s", filename, rocky_result.error)

                # Optional BallCam.tv upload
                if getattr(self.config, "ballcam_enabled", False) and self.config.has_ballcam_token:
                    self.root.after(0, self._win.set_statusbar,
                                    f"Uploading {i}/{len(entries)}: {upload_name} to BallCam.tv…")
                    ballcam_client = BallcamClient(
                        self.config.ballcam_token,
                        getattr(self.config, "ballcam_visibility", "public"),
                    )
                    ballcam_result = ballcam_client.upload_bytes(upload_name, data, title=title)
                    if ballcam_result.ok:
                        logger.info("BallCam upload successful — %s — %s", filename, ballcam_result.url)
                    else:
                        logger.error("BallCam upload failed — %s — %s", filename, ballcam_result.error)

                self._uploaded_guids.add(guid)
                self.config.add_uploaded_guid(guid)
                self.root.after(0, self._on_upload_done, result)

    def _write_stats_api_tickrate(self) -> None:
        """Set PacketSendRate=1 in DefaultStatsAPI.ini if not already set."""
        try:
            ini = self.config.ini_path()
            if ini is None or not ini.exists():
                return
            text = ini.read_text(encoding="utf-8")
            if "PacketSendRate=1" in text:
                return  # already correct
            import re
            new_text = re.sub(r"PacketSendRate=\d+", "PacketSendRate=1", text)
            if new_text == text:
                return  # key not found — don't touch file
            ini.write_text(new_text, encoding="utf-8")
            logger.info("Set PacketSendRate=1 in %s", ini)
        except Exception as exc:
            logger.warning("Could not update DefaultStatsAPI.ini: %s", exc)

    def _psynet_player_name(self, entry: dict) -> str:
        """Return the in-game player name from the PsyNet match entry.

        Searches the Players list for a name that matches the stored Epic
        display name (ignoring trailing punctuation), then falls back to the
        first player in the list, then to the config display name.
        """
        players = entry.get("_raw_entry", {}).get("Match", {}).get("Players", [])
        if not players:
            return (self.config.epic_display_name or "").rstrip(".").strip()
        display = (self.config.epic_display_name or "").rstrip(".").strip().lower()
        # Prefer exact or prefix match
        for p in players:
            pname = (p.get("PlayerName") or "").strip()
            if pname.lower() == display:
                return pname
        # Fall back to first player (recording player is usually first)
        return (players[0].get("PlayerName") or "").strip() or display

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
            self.root.after(0, self._win.set_bc_status, False)
            return

        def _check():
            client = BallchasingClient(self.config.ballchasing_token)
            ok, name, color = client.verify_token()
            if ok:
                self.root.after(0, self._win.set_bc_status, True, name, color)
            else:
                self.root.after(0, self._win.set_bc_status, False)

        threading.Thread(target=_check, daemon=True, name="bc-verify").start()

    # ── auto-update ───────────────────────────────────────────────────────────

    def _check_for_update_async(self) -> None:
        def _check():
            info = check_for_update()
            if info:
                self.root.after(0, self._win.show_update_banner,
                                info["version"], info["download_url"])
        threading.Thread(target=_check, daemon=True, name="update-check").start()

    def check_for_update_manual(self, on_result) -> None:
        """Manual update check — calls on_result(info_dict_or_None, current_version)
        on the Tk thread. Used by the 'Check for Updates' button.
        """
        from core.updater import VERSION

        def _check():
            info = check_for_update()
            if info:
                self.root.after(0, self._win.show_update_banner,
                                info["version"], info["download_url"])
            self.root.after(0, on_result, info, VERSION)
        threading.Thread(target=_check, daemon=True, name="manual-update-check").start()

    def apply_update(self, download_url: str) -> None:
        """Download the new exe, swap it, and restart. Called from the GUI."""
        def _progress(msg: str) -> None:
            self.root.after(0, self._win.set_statusbar, msg)

        def _do():
            try:
                download_and_install(download_url, progress_cb=_progress)
                # Batch script is now running — quit so it can replace the exe
                self.root.after(0, self.quit)
            except RuntimeError as exc:
                logger.error("Update failed: %s", exc)
                self.root.after(0, self._win.set_statusbar, f"Update failed: {exc}")

        threading.Thread(target=_do, daemon=True, name="apply-update").start()

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
                pystray.MenuItem(
                    lambda item: "Resume Monitoring" if self._paused else "Pause Monitoring",
                    lambda: self.root.after(0, self.toggle_pause),
                ),
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
