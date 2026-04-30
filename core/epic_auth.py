"""Epic Games Store / PsyNet authentication and match history retrieval.

Auth flow (mirrors github.com/Amzd/upload-match-history-to-ballchasing):
  1. User visits EGS auth URL, copies the authorizationCode from the redirect page.
  2. App POSTs code → EGS OAuth → access_token + refresh_token.
  3. App GETs exchange code from EGS, then POSTs it to EOS → eos_access_token.
  4. App POSTs AuthPlayer/v2 to PsyNet (HTTP, HMAC-signed) → WebSocket URL + PsyToken.
  5. App connects via WebSocket and calls GetMatchHistory v1.
  6. App downloads ReplayUrl, uploads bytes to ballchasing.

Refresh tokens are saved to config so the user only logs in once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac_mod
import json
import logging
import time
import uuid
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── EGS constants ──────────────────────────────────────────────────────────────
_EGS_CLIENT_ID     = "34a02cf8f4414e29b15921876da36f9a"
_EGS_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
_EGS_OAUTH_BASE    = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth"
_EGS_USER_AGENT    = "UELauncher/11.0.1-14907503+++Portal+Release-Live Windows/10.0.19041.1.256.64bit"

# ── EOS constants ──────────────────────────────────────────────────────────────
_EOS_AUTH_HEADER   = "eHl6YTc4OTFwNUQ3czlSNkdtNm1vVEhXR2xvZXJwN0I6S25oMThkdTROVmxGcyszdVErWlBwRENWdG8wV1lmNHlYUDgrT2N3VnQxbw=="
_EOS_DEPLOYMENT_ID = "da32ae9c12ae40e8a112c52e1f17f3ba"  # Rocket League

# ── PsyNet constants ───────────────────────────────────────────────────────────
_PSY_BASE_URL     = "https://api.rlpp.psynet.gg/rpc"
_PSY_GAME_VER     = "260420.86069.515605"
_PSY_FEATURE_SET  = "PrimeUpdate58_1"
_PSY_BUILD_ID     = "1273328361"
_PSY_SIG_KEY      = b"c338bd36fb8c42b1a431d30add939fc7"
_PSY_HTTP_AGENT   = f"RL Win/{_PSY_GAME_VER} gzip (x86_64-pc-win32) curl-7.67.0 Schannel"
_PSY_WS_AGENT     = f"RL Win/{_PSY_GAME_VER} gzip"


def get_auth_url() -> str:
    """URL the user must visit to obtain an authorization code."""
    from urllib.parse import quote
    redirect = (
        f"https://www.epicgames.com/id/api/redirect"
        f"?clientId={_EGS_CLIENT_ID}&responseType=code"
    )
    return f"https://www.epicgames.com/id/login?redirectUrl={quote(redirect)}"


class EpicAuthError(Exception):
    pass


class EpicClient:
    """Handles the full EGS → EOS → PsyNet auth chain and match history calls."""

    def __init__(self) -> None:
        self._http = requests.Session()

    # ── public ────────────────────────────────────────────────────────────────

    def login_with_code(self, auth_code: str) -> dict:
        """Exchange a one-time auth code for tokens.

        Returns: {access_token, refresh_token, account_id, display_name}
        """
        return self._egs_token({
            "grant_type": "authorization_code",
            "code": auth_code.strip(),
            "token_type": "eg1",
        })

    def refresh_login(self, refresh_token: str) -> dict:
        """Get a fresh access token using a stored refresh token (same return shape)."""
        return self._egs_token({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "token_type": "eg1",
        })

    def get_latest_unuploaded(
        self,
        access_token: str,
        account_id: str,
        display_name: str,
        uploaded_guids: set,
    ) -> Optional[dict]:
        """Full chain: EOS token → PsyNet → GetMatchHistory → first unuploaded entry.

        Returns {'match_guid': str, 'replay_url': str} or None.
        Raises EpicAuthError on any auth/network failure.
        """
        exchange_code          = self._get_exchange_code(access_token)
        eos_token, eos_acct_id = self._get_eos_token(exchange_code)
        ws_url, psy_token, sid = self._psynet_auth(eos_token, eos_acct_id, display_name)
        matches                = self._get_match_history(ws_url, psy_token, sid, eos_acct_id)

        for entry in matches:
            guid = entry.get("Match", {}).get("MatchGUID", "")
            url  = entry.get("ReplayUrl", "")
            if guid and url and guid not in uploaded_guids:
                logger.info("Found unuploaded match: %s", guid)
                return {"match_guid": guid, "replay_url": url}
        logger.info("No new unuploaded matches found in history (%d total)", len(matches))
        return None

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
                "User-Agent":     _PSY_HTTP_AGENT,
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
        import websocket as _ws

        ws = _ws.create_connection(
            ws_url,
            header=[
                f"PsyBuildID: {_PSY_BUILD_ID}",
                f"User-Agent: {_PSY_WS_AGENT}",
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
