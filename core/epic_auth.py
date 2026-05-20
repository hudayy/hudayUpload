"""Epic Games Store / PsyNet authentication and match history retrieval.

Auth flows
----------
New (device auth, RFC 8628):
  1. start_device_auth() → {device_code, user_code, verification_uri, …}
  2. User opens verification_uri in browser, logs in, approves the request.
  3. poll_device_auth_once(device_code) is called repeatedly until success.
  4. Returns {eos_access_token, eos_refresh_token, account_id, display_name}.
  5. EOS token is refreshed directly with refresh_eos_token(eos_refresh_token).
  6. EOS token → PsyNet AuthPlayer/v2 (HMAC-signed) → WebSocket URL + PsyToken.
  7. WebSocket call GetMatchHistory v1 → replay download → upload to ballchasing.

Legacy (browser code, kept for existing accounts):
  1. Browser opens Epic authorize URL (internal EGS client).
  2. User copies the authorization code shown on the page.
  3. App exchanges code → EGS access_token + refresh_token.
  4. App GETs exchange code, POSTs it to EOS → eos_access_token (RL deployment).
  5–7. Same PsyNet/WebSocket/upload chain.

Tokens are saved to config so the user only authenticates once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac_mod
import json
import logging
import mmap
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
import websocket as _websocket

logger = logging.getLogger(__name__)

# ── EGS constants ──────────────────────────────────────────────────────────────
# Internal EGS launcher client — the only one with exchangeTokenCode permission.
_EGS_CLIENT_ID     = "34a02cf8f4414e29b15921876da36f9a"
_EGS_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
_EGS_OAUTH_BASE    = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth"
_EGS_USER_AGENT    = "UELauncher/11.0.1-14907503+++Portal+Release-Live Windows/10.0.19041.1.256.64bit"

# ── EOS constants ──────────────────────────────────────────────────────────────
_EOS_AUTH_HEADER   = "eHl6YTc4OTFwNUQ3czlSNkdtNm1vVEhXR2xvZXJwN0I6S25oMThkdTROVmxGcyszdVErWlBwRENWdG8wV1lmNHlYUDgrT2N3VnQxbw=="
_EOS_DEPLOYMENT_ID = "da32ae9c12ae40e8a112c52e1f17f3ba"  # Rocket League
_EOS_TOKEN_URL     = "https://api.epicgames.dev/epic/oauth/v2/token"
_EOS_DEVICE_URL    = "https://api.epicgames.dev/epic/oauth/v2/deviceAuthorization"
_EOS_USERINFO_URL  = "https://api.epicgames.dev/epic/oauth/v2/userInfo"

# ── PsyNet constants ───────────────────────────────────────────────────────────
_PSY_BASE_URL    = "https://api.rlpp.psynet.gg/rpc"
_PSY_SIG_KEY     = b"c338bd36fb8c42b1a431d30add939fc7"

# These three are updated at runtime by detect_rl_versions(); the values below
# are last-known fallbacks used only when Launch.log / the RL binary cannot be read.
# Last verified: RL build 23106173, updated 2026-05-12.
_PSY_GAME_VER    = "260506.26700.517210"
_PSY_FEATURE_SET = "PrimeUpdate58_1"
_PSY_BUILD_ID    = "-1652286008"

# Compiled patterns for Launch.log parsing
_RE_LOG_FEATURE_SET = re.compile(r"Using feature set (\S+)")
_RE_LOG_BUILD_ID    = re.compile(r"BuildID:\s*(-?\d+)")
_RE_LOG_GAME_VER    = re.compile(r"GPsyonixBuildID\s+(\d+\.\d+\.\d+)")


def get_auth_url() -> str:
    """Epic login URL — redirects to the clean 'Copy Code' page after sign-in."""
    from urllib.parse import quote
    redirect = (
        f"https://www.epicgames.com/id/api/redirect"
        f"?clientId={_EGS_CLIENT_ID}&responseType=code"
    )
    return f"https://www.epicgames.com/id/login?redirectUrl={quote(redirect)}"


def _find_rl_launch_log() -> "Path | None":
    """Return the path to Rocket League's Launch.log, or None if not found.

    The log is in ``{Documents}\\My Games\\Rocket League\\TAGame\\Logs\\Launch.log``.
    Documents may be redirected to OneDrive, so we use the Windows Shell API to
    resolve the real path instead of assuming ``~\\Documents``.
    """
    try:
        import ctypes, ctypes.wintypes
        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
        # CSIDL_PERSONAL = 5  →  "My Documents"
        ctypes.windll.shell32.SHGetFolderPathW(0, 5, 0, 0, buf)
        docs = Path(buf.value)
    except Exception:
        docs = Path.home() / "Documents"

    log = docs / "My Games" / "Rocket League" / "TAGame" / "Logs" / "Launch.log"
    return log if log.exists() else None


def detect_rl_versions(rl_install_path: str) -> bool:
    """Read RL's Launch.log (and fall back to binary scan) to update PsyNet constants.

    Rocket League writes the current FeatureSet and BuildID to Launch.log on every
    startup.  Parsing that file is far more reliable than binary scanning because the
    PsyNet SDK strings are obfuscated in recent RL builds.

    Updates ``_PSY_FEATURE_SET``, ``_PSY_BUILD_ID``, and ``_PSY_GAME_VER`` globals.
    Returns True when at least one value was successfully read.
    """
    global _PSY_FEATURE_SET, _PSY_BUILD_ID, _PSY_GAME_VER

    # ── Method 1: Parse Launch.log (most reliable) ─────────────────────────
    log_path = _find_rl_launch_log()
    if log_path:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            new_fs  = (m := _RE_LOG_FEATURE_SET.search(text)) and m.group(1)
            new_bid = (m := _RE_LOG_BUILD_ID.search(text))    and m.group(1)
            new_ver = (m := _RE_LOG_GAME_VER.search(text))    and m.group(1)

            changed = False
            if new_fs and new_fs != _PSY_FEATURE_SET:
                logger.info("PsyNet FeatureSet: %s → %s", _PSY_FEATURE_SET, new_fs)
                _PSY_FEATURE_SET = new_fs
                changed = True
            if new_bid and new_bid != _PSY_BUILD_ID:
                logger.info("PsyNet BuildID: %s → %s", _PSY_BUILD_ID, new_bid)
                _PSY_BUILD_ID = new_bid
                changed = True
            if new_ver and new_ver != _PSY_GAME_VER:
                logger.info("PsyNet GameVer: %s → %s", _PSY_GAME_VER, new_ver)
                _PSY_GAME_VER = new_ver
                changed = True

            if new_fs or new_bid or new_ver:
                level = logging.INFO if changed else logging.DEBUG
                logger.log(
                    level,
                    "PsyNet constants from Launch.log (FeatureSet=%s, BuildID=%s, GameVer=%s)",
                    _PSY_FEATURE_SET, _PSY_BUILD_ID, _PSY_GAME_VER,
                )
                return True
            logger.warning("detect_rl_versions: Launch.log found but no PsyNet fields parsed")
        except Exception as exc:
            logger.warning("detect_rl_versions: failed to read Launch.log — %s", exc)

    # ── Method 2: Binary scan (fallback — only recovers FeatureSet) ─────────
    # PsyNet SDK strings are UTF-16 in recent RL builds; BuildID is obfuscated.
    try:
        # Accept both the root install dir and the TAGame subdir in config
        candidates = [Path(rl_install_path)]
        if Path(rl_install_path).name.lower() == "tagame":
            candidates.append(Path(rl_install_path).parent)
        candidates.append(Path(rl_install_path).parent)  # always try parent too

        bin_path = None
        for base in candidates:
            p = base / "Binaries" / "Win64" / "RocketLeague.exe"
            if p.exists():
                bin_path = p
                break

        if bin_path is None:
            logger.warning(
                "detect_rl_versions: RL binary not found near %s — using built-in fallbacks",
                rl_install_path,
            )
            return False

        needle_utf16 = "PrimeUpdate".encode("utf-16-le")
        pat_utf16    = re.compile(rb"(?:PrimeUpdate\d+(?:_\d+)?)")

        with open(bin_path, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                data = bytes(mm)

        # Collect all UTF-16 PrimeUpdate strings, take the last one (highest version)
        pos, last_fs = 0, None
        while True:
            idx = data.find(needle_utf16, pos)
            if idx < 0:
                break
            end = idx
            while end + 1 < len(data) and (data[end] != 0 or data[end + 1] != 0):
                end += 2
            s = data[idx:end].decode("utf-16-le", errors="replace")
            if re.match(r"PrimeUpdate\d+", s):
                last_fs = s
            pos = idx + 2

        if last_fs and last_fs != _PSY_FEATURE_SET:
            logger.info("PsyNet FeatureSet (binary): %s → %s", _PSY_FEATURE_SET, last_fs)
            _PSY_FEATURE_SET = last_fs
            logger.info(
                "PsyNet constants partially updated from binary (FeatureSet=%s, BuildID unchanged=%s)",
                _PSY_FEATURE_SET, _PSY_BUILD_ID,
            )
            return True
        elif last_fs:
            logger.debug("PsyNet FeatureSet confirmed from binary: %s", last_fs)
            return True
        else:
            logger.warning("detect_rl_versions: no FeatureSet found in binary")
            return False

    except Exception as exc:
        logger.warning("detect_rl_versions: binary scan failed — %s", exc)
        return False


# ── Device Authorization Grant (RFC 8628 via EOS) ────────────────────────────

def start_device_auth() -> dict:
    """Begin the OAuth 2.0 Device Authorization Grant flow.

    Returns a dict with:
      device_code      – pass to poll_device_auth_once() in a loop
      user_code        – 8-character code the user enters on the browser page
      verification_uri – URL to open (verification_uri_complete when available,
                         so the code is pre-filled and the user just logs in)
      expires_in       – seconds until device_code expires
      interval         – minimum seconds between poll_device_auth_once() calls

    Raises EpicAuthError on network or server errors.
    """
    resp = requests.post(
        _EOS_DEVICE_URL,
        data={"client_id": "xyza7891p5D7s9R6Gm6moTHWGloerp7B"},
        headers={
            "Authorization":  f"Basic {_EOS_AUTH_HEADER}",
            "Content-Type":   "application/x-www-form-urlencoded",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise EpicAuthError(
            f"Device auth start failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    data = resp.json()
    # verification_uri_complete has the user_code pre-filled — best for same-device use
    return {
        "device_code":     data["device_code"],
        "user_code":       data.get("user_code", ""),
        "verification_uri": (
            data.get("verification_uri_complete") or data.get("verification_uri", "")
        ),
        "expires_in":      int(data.get("expires_in", 600)),
        "interval":        float(data.get("interval", 5)),
    }


def poll_device_auth_once(device_code: str) -> "dict | None":
    """Poll the EOS token endpoint once for device auth completion.

    Returns:
      - dict {eos_access_token, eos_refresh_token, account_id, display_name}
        when the user has approved in their browser.
      - None when still pending or when the server says to slow down
        (caller should wait `interval` seconds and retry).

    Raises EpicAuthError on authorization_denied or any other terminal error.
    """
    resp = requests.post(
        _EOS_TOKEN_URL,
        data={
            "grant_type":    "device_code",
            "device_code":   device_code,
            "deployment_id": _EOS_DEPLOYMENT_ID,
            "scope":         "basic_profile",
        },
        headers={
            "Authorization":  f"Basic {_EOS_AUTH_HEADER}",
            "Content-Type":   "application/x-www-form-urlencoded",
        },
        timeout=30,
    )
    data = resp.json()
    if resp.status_code == 200:
        access_token  = data["access_token"]
        refresh_token = data.get("refresh_token", "")
        account_id    = data.get("account_id", "")
        display_name  = _fetch_eos_display_name(access_token) or account_id
        return {
            "eos_access_token":  access_token,
            "eos_refresh_token": refresh_token,
            "account_id":        account_id,
            "display_name":      display_name,
        }
    error_code = data.get("errorCode", "")
    if "authorization_pending" in error_code or "slow_down" in error_code:
        return None  # still waiting — caller retries
    msg = data.get("errorMessage") or data.get("error_description") or resp.text[:200]
    raise EpicAuthError(f"Device authorization failed: {msg}")


def _fetch_eos_display_name(eos_access_token: str) -> str:
    """GET /epic/oauth/v2/userInfo → preferred_username, or '' on any failure."""
    try:
        resp = requests.get(
            _EOS_USERINFO_URL,
            headers={"Authorization": f"Bearer {eos_access_token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            d = resp.json()
            return d.get("preferred_username") or d.get("name") or ""
    except Exception:
        pass
    return ""


class EpicAuthError(Exception):
    pass


class EpicClient:
    """Handles the full EGS → EOS → PsyNet auth chain and match history calls."""

    def __init__(self) -> None:
        self._http = requests.Session()

    # ── public ────────────────────────────────────────────────────────────────

    def open_auth_browser(self) -> None:
        """Open the Epic authorize page in the default browser."""
        import webbrowser
        webbrowser.open(get_auth_url())

    def login_with_code(self, auth_code: str) -> dict:
        """Exchange a one-time auth code for tokens.

        Returns: {access_token, refresh_token, account_id, display_name}
        """
        return self._egs_token({
            "grant_type": "authorization_code",
            "code":        auth_code.strip(),
            "token_type":  "eg1",
        })

    def refresh_login(self, refresh_token: str) -> dict:
        """Get a fresh EGS access token using a stored EGS refresh token (same return shape)."""
        return self._egs_token({
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "token_type":    "eg1",
        })

    def refresh_eos_token(self, eos_refresh_token: str) -> dict:
        """Refresh an EOS refresh token obtained via the device auth flow.

        Returns {eos_access_token, eos_refresh_token, account_id, display_name}.
        The new eos_refresh_token should be saved back to config.
        Raises EpicAuthError when the token has expired (user must re-authenticate).
        """
        resp = self._http.post(
            _EOS_TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": eos_refresh_token,
                "deployment_id": _EOS_DEPLOYMENT_ID,
                "scope":         "basic_profile",
            },
            headers={
                "Authorization":  f"Basic {_EOS_AUTH_HEADER}",
                "Content-Type":   "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            data = resp.json()
            msg = data.get("errorMessage") or data.get("error_description") or resp.text[:200]
            raise EpicAuthError(f"EOS token refresh failed: {msg}")
        data = resp.json()
        access_token = data["access_token"]
        return {
            "eos_access_token":  access_token,
            # Server may rotate the refresh token; fall back to the old one if not
            "eos_refresh_token": data.get("refresh_token") or eos_refresh_token,
            "account_id":        data.get("account_id", ""),
            "display_name":      _fetch_eos_display_name(access_token),
        }

    def get_unuploaded_matches(
        self,
        access_token: str,
        account_id: str,
        display_name: str,
        uploaded_guids: set,
        max_count: int = 5,
    ) -> list[dict]:
        """Legacy EGS flow: exchange code → EOS token → PsyNet → match history.

        Returns a list of match dicts (oldest-first), up to max_count entries.
        Raises EpicAuthError on any auth/network failure.
        """
        exchange_code          = self._get_exchange_code(access_token)
        eos_token, eos_acct_id = self._get_eos_token(exchange_code)
        ws_url, psy_token, sid = self._psynet_auth(eos_token, eos_acct_id, display_name)
        matches                = self._get_match_history(ws_url, psy_token, sid, eos_acct_id)
        return self._collect_unuploaded(matches, uploaded_guids, max_count, display_name)

    def get_unuploaded_matches_from_eos(
        self,
        eos_access_token: str,
        account_id: str,
        display_name: str,
        uploaded_guids: set,
        max_count: int = 5,
    ) -> list[dict]:
        """Device-auth flow: EOS token → PsyNet → match history (no EGS exchange needed).

        Returns a list of match dicts (oldest-first), up to max_count entries.
        Raises EpicAuthError on any auth/network failure.
        """
        ws_url, psy_token, sid = self._psynet_auth(eos_access_token, account_id, display_name)
        matches                = self._get_match_history(ws_url, psy_token, sid, account_id)
        return self._collect_unuploaded(matches, uploaded_guids, max_count, display_name)

    def _collect_unuploaded(
        self,
        matches: list,
        uploaded_guids: set,
        max_count: int,
        display_name: str = "",
    ) -> list[dict]:
        """Filter a PsyNet match list to unuploaded entries, returning oldest-first."""
        found = []
        for entry in matches:
            match = entry.get("Match", {})
            guid  = match.get("MatchGUID", "")
            url   = entry.get("ReplayUrl", "")
            if guid and url and guid not in uploaded_guids:
                # Log the raw entry so field names can be verified in exported logs
                logger.info("Raw PsyNet match entry for %s: %s", guid, entry)
                found.append({
                    "match_guid":     guid,
                    "replay_url":     url,
                    "display_name":   display_name,
                    # Extra fields for building a human-readable title.
                    "match_time":     match.get("Created", ""),
                    "playlist_id":    match.get("Playlist", 0),
                    "player_team_id": entry.get("PlayerTeamID", -1),
                    "teams":          match.get("Teams", []),
                    # Keep full raw entry so app.py can read any field
                    "_raw_entry":     entry,
                })
                if len(found) >= max_count:
                    break

        if found:
            logger.info("Found %d unuploaded match(es) (limit=%d)", len(found), max_count)
        else:
            logger.info("No new unuploaded matches found in history (%d total)", len(matches))

        return list(reversed(found))  # oldest first

    def download_replay(self, url: str) -> bytes:
        """Download replay bytes from the given URL."""
        resp = requests.get(url, timeout=120)
        if resp.status_code != 200:
            raise EpicAuthError(f"Replay download failed: HTTP {resp.status_code}")
        return resp.content

    # ── EGS OAuth ─────────────────────────────────────────────────────────────

    def _egs_token(self, params: dict) -> dict:
        basic = base64.b64encode(
            f"{_EGS_CLIENT_ID}:{_EGS_CLIENT_SECRET}".encode()
        ).decode()
        resp = self._http.post(
            f"{_EGS_OAUTH_BASE}/token",
            data=params,
            headers={
                "Authorization": f"Basic {basic}",
                "User-Agent": _EGS_USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        data = resp.json()
        if resp.status_code != 200:
            msg = data.get("errorMessage") or data.get("error_description") or resp.text[:300]
            raise EpicAuthError(f"EGS auth failed: {msg}")
        return {
            "access_token":  data["access_token"],
            "refresh_token": data["refresh_token"],
            "account_id":    data.get("account_id", ""),
            "display_name":  data.get("displayName", ""),
        }

    def _get_exchange_code(self, access_token: str) -> str:
        resp = self._http.get(
            f"{_EGS_OAUTH_BASE}/exchange",
            headers={
                "Authorization": f"bearer {access_token}",
                "User-Agent": _EGS_USER_AGENT,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise EpicAuthError(f"Exchange code failed: HTTP {resp.status_code} — {resp.text[:200]}")
        return resp.json()["code"]

    def _get_eos_token(self, exchange_code: str) -> tuple[str, str]:
        resp = self._http.post(
            "https://api.epicgames.dev/epic/oauth/v2/token",
            data={
                "grant_type":    "exchange_code",
                "exchange_code": exchange_code,
                "deployment_id": _EOS_DEPLOYMENT_ID,
                "scope":         "basic_profile",
            },
            headers={
                "Authorization":  f"Basic {_EOS_AUTH_HEADER}",
                "User-Agent":     _EGS_USER_AGENT,
                "Content-Type":   "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise EpicAuthError(f"EOS token failed: HTTP {resp.status_code} — {resp.text[:200]}")
        data = resp.json()
        return data["access_token"], data["account_id"]

    # ── PsyNet HTTP auth ──────────────────────────────────────────────────────

    def _psynet_auth(
        self, eos_token: str, account_id: str, display_name: str
    ) -> tuple[str, str, str]:
        """POST AuthPlayer/v2 → returns (ws_url, psy_token, session_id)."""
        body_obj = {
            "Platform":            "Epic",
            "PlayerName":          display_name,
            "PlayerID":            account_id,
            "Language":            "INT",
            "AuthTicket":          eos_token,
            "BuildRegion":         "",
            "FeatureSet":          _PSY_FEATURE_SET,
            "Device":              "PC",
            "LocalFirstPlayerID":  f"Epic|{account_id}|0",
            "bSkipAuth":           False,
            "bSetAsPrimaryAccount": True,
            "EpicAuthTicket":      eos_token,
            "EpicAccountID":       account_id,
        }
        body_bytes = json.dumps(body_obj, separators=(",", ":")).encode()
        request_id = str(uuid.uuid4())

        resp = self._http.post(
            f"{_PSY_BASE_URL}/Auth/AuthPlayer/v2",
            data=body_bytes,
            headers={
                "Content-Type":   "application/x-www-form-urlencoded",
                "User-Agent":     f"RL Win/{_PSY_GAME_VER} gzip (x86_64-pc-win32) curl-7.67.0 Schannel",
                "PsyBuildID":     _PSY_BUILD_ID,
                "PsyEnvironment": "Prod",
                "PsyRequestID":   request_id,
                "PsySig":         _psy_sig(body_bytes),
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise EpicAuthError(
                f"PsyNet AuthPlayer failed: HTTP {resp.status_code} — {resp.text[:300]}"
            )
        wrapper = resp.json()
        err = wrapper.get("Error")
        if err:
            raise EpicAuthError(f"PsyNet error: {err}")

        result = wrapper["Result"]
        return result["PerConURLv2"], result["PsyToken"], result["SessionID"]

    # ── PsyNet WebSocket ──────────────────────────────────────────────────────

    def _get_match_history(
        self, ws_url: str, psy_token: str, session_id: str, account_id: str
    ) -> list:
        """Connect via WebSocket and call GetMatchHistory v1."""
        ws = _websocket.create_connection(
            ws_url,
            header=[
                f"PsyBuildID: {_PSY_BUILD_ID}",
                f"User-Agent: RL Win/{_PSY_GAME_VER} gzip",
                "PsyEnvironment: Prod",
                f"PsyToken: {psy_token}",
                f"PsySessionID: {session_id}",
            ],
            timeout=30,
        )
        try:
            request_id = "1"
            body_obj   = {"PlayerID": f"Epic|{account_id}|0"}
            body_bytes = json.dumps(body_obj, separators=(",", ":")).encode()

            # Build the PsyNet text message: headers + blank line + JSON body
            message = (
                f"PsyService: Matches/GetMatchHistory v1\r\n"
                f"PsyRequestID: {request_id}\r\n"
                f"PsySig: {_psy_sig(body_bytes)}\r\n"
                f"\r\n"
                + body_bytes.decode()
            )
            ws.send(message)
            logger.debug("Sent GetMatchHistory request")

            deadline = time.time() + 20
            while time.time() < deadline:
                ws.settimeout(5)
                try:
                    raw = ws.recv()
                except Exception:
                    break
                if not raw:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if raw.startswith("PsyPong"):
                    continue

                # Parse response: headers \r\n\r\n json
                delim = "\r\n\r\n"
                idx = raw.find(delim)
                if idx == -1:
                    continue

                hdrs = {}
                for line in raw[:idx].split("\r\n"):
                    if ":" in line:
                        k, _, v = line.partition(":")
                        hdrs[k.strip()] = v.strip()

                if hdrs.get("PsyResponseID") != request_id:
                    continue

                try:
                    data = json.loads(raw[idx + len(delim):])
                except json.JSONDecodeError:
                    continue

                if data.get("Error"):
                    raise EpicAuthError(f"GetMatchHistory error: {data['Error']}")

                matches = data.get("Result", {}).get("Matches", [])
                logger.info("GetMatchHistory returned %d entries", len(matches))
                return matches

        finally:
            try:
                ws.close()
            except Exception:
                pass

        logger.warning("GetMatchHistory timed out waiting for response")
        return []


# ── helpers ────────────────────────────────────────────────────────────────────

def _psy_sig(body_bytes: bytes) -> str:
    """HMAC-SHA256(key, '-' + body), base64-encoded — matches the Go implementation."""
    h = _hmac_mod.new(_PSY_SIG_KEY, b"-" + body_bytes, hashlib.sha256)
    return base64.b64encode(h.digest()).decode()
