"""Application coordinator — bridges GUI, Stats API watcher, and uploader."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from core.config import Config
from core.epic_auth import EpicAuthError, EpicClient, detect_rl_versions
from core.replay_meta import build_title, parse_header, write_replay_name
from core.stats_watcher import StatsWatcher
from core.updater import check_for_update, download_and_install
from core.uploader import BallchasingClient, RockyClient, UploadResult

logger = logging.getLogger(__name__)

_POST_GAME_DELAY_DEFAULT = 30.0  # fallback if config not loaded yet


def _is_rl_running() -> bool:
    """Return True if RocketLeague.exe is in the process list."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RocketLeague.exe", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "RocketLeague.exe" in out.stdout
    except Exception:
        return False



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
        self._rl_close_watcher_running: bool = False
        # Close-watcher used when Stats API is fully disabled in settings
        self._no_statsapi_watcher_running: bool = False
        # Current RL player name (detected via Stats API, for display only)
        self._current_rl_player: str = ""

    # ── startup / teardown ────────────────────────────────────────────────────

    def start(self) -> None:
        from gui.main_window import MainWindow
        self._win = MainWindow(self.root, self)

        self._start_tray()  # must run first so self._tray is set

        # Only hide to tray if the tray actually started
        if self.config.start_minimized and self._tray is not None:
            self.root.withdraw()

        if self.config.stats_api_enabled:
            self._watcher.start()
        else:
            self._win.set_stats_api_enabled(False)
            self._ensure_no_statsapi_close_watcher()
        self._start_event_pump()
        self._detect_rl_versions_async()
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
        """Immediately fetch latest unuploaded matches from Epic (all accounts) and upload."""
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
        if self.config.stats_api_enabled and not self._paused:
            self._watcher.start()
        else:
            # Stats API just turned off (or was already off) — ensure we still
            # detect RL closing so auto-upload keeps working
            if not self.config.stats_api_enabled:
                self._ensure_no_statsapi_close_watcher()
        self.root.after(0, self._win.set_stats_api_enabled, self.config.stats_api_enabled)
        self._detect_rl_versions_async()
        self._verify_bc_token_async()
        self._refresh_epic_status_ui()

    def toggle_pause(self) -> None:
        """Pause or resume the Stats API connection to reduce in-game CPU load."""
        if not self.config.stats_api_enabled:
            return  # can't pause/resume when the Stats API is disabled in settings
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
            # Watch for RL closing while paused so we can still auto-upload
            self._ensure_rl_close_watcher()

    def _ensure_rl_close_watcher(self) -> None:
        """Start a background thread that detects RL exit while monitoring is paused."""
        if self._rl_close_watcher_running:
            return
        self._rl_close_watcher_running = True
        threading.Thread(
            target=self._rl_close_watch_loop,
            daemon=True, name="rl-close-watch",
        ).start()

    def _rl_close_watch_loop(self) -> None:
        """Poll for RocketLeague.exe; when it exits trigger upload if games are pending."""
        try:
            was_running = _is_rl_running()
            while self._paused:
                now_running = _is_rl_running()
                if was_running and not now_running:
                    # RL just closed while we were paused
                    if self.config.auto_upload:
                        count = self._games_since_upload
                        self._games_since_upload = 0
                        delay = float(getattr(self.config, "post_game_delay",
                                              _POST_GAME_DELAY_DEFAULT))
                        msg = (
                            f"Rocket League closed — uploading {count} game(s)…"
                            if count > 0
                            else "Rocket League closed — checking for new replays…"
                        )
                        logger.info("RL closed while paused — triggering upload (pending=%d)", count)
                        self.root.after(0, self._win.set_statusbar, msg)
                        threading.Thread(
                            target=self._run_epic_upload,
                            args=(delay,), daemon=True, name="auto-upload-paused",
                        ).start()
                    break
                was_running = now_running
                time.sleep(5)
        finally:
            self._rl_close_watcher_running = False

    def _ensure_no_statsapi_close_watcher(self) -> None:
        """Start the Stats-API-off RL close watcher if it isn't already running."""
        if self._no_statsapi_watcher_running:
            return
        self._no_statsapi_watcher_running = True
        threading.Thread(
            target=self._no_statsapi_close_watch_loop,
            daemon=True, name="rl-close-watch-nostats",
        ).start()

    def _no_statsapi_close_watch_loop(self) -> None:
        """Poll for RL exit when the Stats API is disabled.

        Keeps looping (detecting each RL session) until Stats API is re-enabled.
        Each time RL closes, triggers auto-upload so the user doesn't have to
        hit 'Upload Now' manually.
        """
        try:
            was_running = _is_rl_running()
            while not self.config.stats_api_enabled:
                time.sleep(5)
                if self.config.stats_api_enabled:
                    break
                now_running = _is_rl_running()
                if was_running and not now_running:
                    # RL just closed
                    if self.config.auto_upload:
                        delay = float(getattr(self.config, "post_game_delay",
                                              _POST_GAME_DELAY_DEFAULT))
                        logger.info(
                            "RL closed (Stats API disabled) — triggering upload"
                        )
                        self.root.after(0, self._win.set_statusbar,
                                        "Rocket League closed — checking for new replays…")
                        threading.Thread(
                            target=self._run_epic_upload,
                            args=(delay,), daemon=True, name="auto-upload-nostats",
                        ).start()
                was_running = now_running
        finally:
            self._no_statsapi_watcher_running = False

    def _refresh_epic_status_ui(self) -> None:
        accounts = self.config.get_epic_accounts()
        if not accounts:
            self.root.after(0, self._win.set_epic_status, False,
                            "Not connected — open ⚙ Settings")
        elif len(accounts) == 1:
            name = accounts[0].get("display_name", "").strip()
            self.root.after(0, self._win.set_epic_status, True,
                            f"Connected as {name}" if name else "Connected")
        else:
            names = [a.get("display_name", "").strip() or a.get("account_id", "?")
                     for a in accounts]
            self.root.after(0, self._win.set_epic_status, True,
                            f"Connected: {', '.join(names)}")

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
            self._current_rl_player = ""
            self._win.set_rl_status(None, "Waiting for Rocket League…")
            # Revert Epic status back to account summary (stop showing "Playing as …")
            self._refresh_epic_status_ui()
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

            # Show who's currently playing in the Epic Games status row
            primary_name = ""
            for p in msg.get("players", []):
                if p.get("primary"):
                    primary_name = p.get("name", "")
                    break

            if primary_name and primary_name != self._current_rl_player:
                self._current_rl_player = primary_name
                logger.info("RL primary player: %s", primary_name)
                self._win.set_epic_status(True, f"Playing as {primary_name}")
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
        """Wait `delay` seconds, then fetch + download + upload for ALL Epic accounts."""
        if delay > 0:
            logger.info("Waiting %.0f seconds before fetching replay…", delay)
            time.sleep(delay)

        with self._uploading_lock:
            if not self.config.has_bc_token:
                logger.warning("Upload skipped — no Ballchasing token configured")
                self.root.after(0, self._win.set_statusbar,
                                "No Ballchasing token — open ⚙ Settings.")
                return

            accounts = self.config.get_epic_accounts()
            if not accounts:
                logger.warning("Upload skipped — no Epic accounts configured")
                self.root.after(0, self._win.set_statusbar,
                                "Not logged in to Epic — open ⚙ Settings to connect.")
                return

            self.root.after(0, self._win.set_statusbar, "Fetching matches from Epic API…")

            batch_size = int(getattr(self.config, "upload_batch_size", 5))
            all_entries: list[dict] = []

            # ── Collect unuploaded matches from every account ──────────────
            for account in accounts:
                acc_label = account.get("display_name") or account.get("account_id", "?")
                eos_rt    = account.get("eos_refresh_token", "")
                egs_rt    = account.get("refresh_token", "")

                if eos_rt:
                    # ── Device-auth path: refresh EOS token directly ───────
                    logger.info("Refreshing EOS token for %s", acc_label)
                    try:
                        eos_data = self._epic.refresh_eos_token(eos_rt)
                    except EpicAuthError as exc:
                        logger.error("EOS token refresh failed for %s: %s", acc_label, exc)
                        self.root.after(0, self._win.set_statusbar,
                                        f"Epic session expired for {acc_label} — "
                                        f"re-add account in Settings. ({exc})")
                        continue
                    except Exception as exc:
                        logger.error("Network error refreshing EOS token for %s: %s", acc_label, exc)
                        self.root.after(0, self._win.set_statusbar,
                                        f"Network error ({acc_label}) — will retry next game: {exc}")
                        continue

                    self.config.update_epic_account_eos_token(
                        account["account_id"],
                        eos_data["eos_refresh_token"],
                        eos_data.get("display_name") or acc_label,
                    )
                    display_name = eos_data.get("display_name") or acc_label

                    logger.info("Fetching PsyNet history for %s (EOS path, batch=%d)",
                                acc_label, batch_size)
                    try:
                        entries = self._epic.get_unuploaded_matches_from_eos(
                            eos_access_token = eos_data["eos_access_token"],
                            account_id       = eos_data["account_id"],
                            display_name     = display_name,
                            uploaded_guids   = self._uploaded_guids,
                            max_count        = batch_size,
                        )
                    except EpicAuthError as exc:
                        logger.error("PsyNet history failed for %s: %s", acc_label, exc)
                        self.root.after(0, self._win.set_statusbar,
                                        f"Epic API error ({acc_label}): {exc}")
                        continue
                    except Exception as exc:
                        logger.error("Network error fetching history for %s: %s", acc_label, exc)
                        self.root.after(0, self._win.set_statusbar,
                                        f"Network error ({acc_label}) — will retry next game: {exc}")
                        continue

                    all_entries.extend(entries)

                elif egs_rt:
                    # ── Legacy path: refresh EGS token → EOS exchange ──────
                    logger.info("Refreshing EGS token for %s", acc_label)
                    try:
                        token_data = self._epic.refresh_login(egs_rt)
                        logger.info("EGS token refreshed — %s",
                                    token_data.get("display_name") or token_data.get("account_id"))
                    except EpicAuthError as exc:
                        logger.error("EGS token refresh failed for %s: %s", acc_label, exc)
                        self.root.after(0, self._win.set_statusbar,
                                        f"Epic login expired for {acc_label} — "
                                        f"re-authenticate in Settings. ({exc})")
                        continue
                    except Exception as exc:
                        logger.error("Network error refreshing EGS token for %s: %s", acc_label, exc)
                        self.root.after(0, self._win.set_statusbar,
                                        f"Network error ({acc_label}) — will retry next game: {exc}")
                        continue

                    self.config.update_epic_account_token(
                        account["account_id"],
                        token_data["refresh_token"],
                        token_data.get("display_name") or acc_label,
                    )
                    display_name = token_data.get("display_name") or acc_label

                    logger.info("Fetching PsyNet history for %s (EGS path, batch=%d)",
                                acc_label, batch_size)
                    try:
                        entries = self._epic.get_unuploaded_matches(
                            access_token   = token_data["access_token"],
                            account_id     = token_data["account_id"],
                            display_name   = display_name,
                            uploaded_guids = self._uploaded_guids,
                            max_count      = batch_size,
                        )
                    except EpicAuthError as exc:
                        logger.error("PsyNet history failed for %s: %s", acc_label, exc)
                        self.root.after(0, self._win.set_statusbar,
                                        f"Epic API error ({acc_label}): {exc}")
                        continue
                    except Exception as exc:
                        logger.error("Network error fetching history for %s: %s", acc_label, exc)
                        self.root.after(0, self._win.set_statusbar,
                                        f"Network error ({acc_label}) — will retry next game: {exc}")
                        continue

                    all_entries.extend(entries)

                else:
                    logger.warning("Account %s has no refresh token — skipping", acc_label)
                    continue

            if not all_entries:
                logger.info("No new unuploaded matches found across all accounts")
                self.root.after(0, self._win.set_statusbar,
                                "No new matches found in Epic history — try Upload Now later.")
                return

            # ── Upload all collected entries ───────────────────────────────
            client = BallchasingClient(
                self.config.ballchasing_token,
                self.config.ballchasing_visibility,
            )

            for i, entry in enumerate(all_entries, 1):
                guid         = entry["match_guid"]
                replay_url   = entry["replay_url"]
                # display_name is now stored per-entry by _collect_unuploaded
                display_name = entry.get("display_name", "")
                filename     = f"{guid}.replay"

                self.root.after(0, self._win.set_statusbar,
                                f"Downloading replay {i}/{len(all_entries)}: {filename}…")
                logger.info("Downloading replay %s (%d/%d)", filename, i, len(all_entries))
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
                    props["PlayerName"] = self._psynet_player_name(entry, display_name)
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
                    my_name = display_name.rstrip(".").strip().lower()
                    for p in raw_match.get("Players", []):
                        if (p.get("PlayerName") or "").strip().lower() == my_name:
                            props["PrimaryPlayerTeam"] = p.get("LastTeam", -1)
                            break

                # Supplement with Stats API UpdateState data if available
                state = self._match_states.get(guid)
                if state:
                    if not isinstance(props.get("PrimaryPlayerTeam"), int):
                        my_name = display_name.rstrip(".").strip().lower()
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

                title = build_title(props, fallback_name=display_name)

                # Write the title into the replay's ReplayName binary property
                if title:
                    data = write_replay_name(data, title)

                # Use the title as the filename — ballchasing sets the replay's
                # display name from the uploaded filename, no API PATCH needed.
                upload_name = f"{title}.replay" if title else filename
                logger.info("Uploading %s as %r (visibility=%s)",
                            filename, upload_name, self.config.ballchasing_visibility)
                self.root.after(0, self._win.set_statusbar,
                                f"Uploading {i}/{len(all_entries)}: {upload_name} to ballchasing…")
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
                                    f"Uploading {i}/{len(all_entries)}: {upload_name} to Rocky…")
                    rocky_result = RockyClient().upload_bytes(upload_name, data)
                    if rocky_result.ok and not rocky_result.duplicate:
                        logger.info("Rocky upload successful — %s", filename)
                    elif rocky_result.duplicate:
                        logger.info("Rocky: already uploaded (duplicate) — %s", filename)
                    else:
                        logger.error("Rocky upload failed — %s — %s", filename, rocky_result.error)

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

    def _psynet_player_name(self, entry: dict, display_name: str = "") -> str:
        """Return the in-game player name from the PsyNet match entry.

        Searches the Players list for a name that matches the active Epic
        display name (ignoring trailing punctuation), then falls back to the
        first player in the list, then to the display_name itself.
        """
        players = entry.get("_raw_entry", {}).get("Match", {}).get("Players", [])
        clean = display_name.rstrip(".").strip()
        if not players:
            return clean
        d_lower = clean.lower()
        for p in players:
            pname = (p.get("PlayerName") or "").strip()
            if pname.lower() == d_lower:
                return pname
        # Fall back to first player (recording player is usually first)
        return (players[0].get("PlayerName") or "").strip() or clean

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

    # ── RL version detection ──────────────────────────────────────────────────

    def _detect_rl_versions_async(self) -> None:
        """Scan the RL binary in a background thread to update PsyNet constants.

        Called at startup and whenever the RL install path changes in Settings.
        If the path is empty or the binary is absent, falls back to the built-in
        constants (which worked for the last known RL version).
        """
        install_path = self.config.rl_install_path
        if not install_path:
            logger.debug(
                "RL install path not configured — skipping PsyNet version detection"
            )
            return

        def _scan() -> None:
            ok = detect_rl_versions(install_path)
            if not ok:
                logger.warning(
                    "PsyNet version detection failed — built-in fallbacks will be used; "
                    "if you see VersionMismatch errors, verify the RL install path in Settings"
                )

        threading.Thread(target=_scan, daemon=True, name="rl-version-scan").start()

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
        """Download the new exe, swap it via batch script, then quit."""
        def _progress(msg: str) -> None:
            self.root.after(0, self._win.set_statusbar, msg)

        def _do():
            try:
                download_and_install(download_url, progress_cb=_progress)
                # Batch script is running in the background waiting for us to exit.
                # Show a brief notice then quit so the swap can proceed.
                def _finish():
                    import tkinter.messagebox as _mb
                    _mb.showinfo(
                        "Update Ready",
                        "The update has been downloaded.\n\n"
                        "hudayUpload will now close — please reopen it to use the new version.",
                    )
                    self.quit()
                self.root.after(0, _finish)
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
                    lambda item: (
                        "Stats API Disabled" if not self.config.stats_api_enabled
                        else ("Resume Monitoring" if self._paused else "Pause Monitoring")
                    ),
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
