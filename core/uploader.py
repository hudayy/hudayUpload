"""Find the newest replay and upload it to ballchasing.com."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BC_UPLOAD_URL      = "https://ballchasing.com/api/v2/upload"
_BC_PING_URL        = "https://ballchasing.com/api/"
_ROCKY_UPLOAD_URL   = "https://lexore.ca/rocky/api/upload"
_BALLCAM_UPLOAD_URL = "https://api.ballcam.tv/replays"


@dataclass
class UploadResult:
    ok: bool
    duplicate: bool = False
    replay_id: str = ""
    url: str = ""
    filename: str = ""
    error: str = ""
    timestamp: float = field(default_factory=time.time)


class BallchasingClient:
    def __init__(self, token: str, visibility: str = "public") -> None:
        self.token = token
        self.visibility = visibility
        self._session = requests.Session()
        self._session.headers["Authorization"] = token

    # ── auth ─────────────────────────────────────────────────────────────────

    # Tier → hex color (matches ballchasing.com/upload palette)
    _TIER_COLORS: dict[str, str] = {
        "legend":         "#FF8FC1",
        "gc":             "#F540EC",
        "champion":       "#9982BA",
        "diamond":        "#0579FF",
        "gold":           "#B8860B",
        "free":           "#767676",
        "":               "#767676",
    }

    def verify_token(self) -> tuple[bool, str, str]:
        """Return (ok, name, tier_color).

        name       — display name from ballchasing (empty string on failure)
        tier_color — hex color for the tier, or error message on failure
        """
        try:
            r = self._session.get(_BC_PING_URL, timeout=10)
            if r.status_code == 200:
                data = r.json()
                name = data.get("steam_name") or data.get("name", "")
                tier = (data.get("type") or "").lower()
                color = self._TIER_COLORS.get(tier, self._TIER_COLORS["free"])
                return True, name, color
            return False, f"HTTP {r.status_code}", ""
        except requests.RequestException as exc:
            return False, str(exc), ""

    # ── upload ────────────────────────────────────────────────────────────────

    def upload_bytes(self, filename: str, data: bytes) -> UploadResult:
        """Upload replay from in-memory bytes (downloaded from Epic API).

        Pass the desired display name as *filename* (e.g. '2026-04-28.00.09 huday
        Ranked Doubles Win.replay') — ballchasing uses the uploaded filename as
        the replay title automatically, no separate PATCH required.
        """
        try:
            r = self._session.post(
                _BC_UPLOAD_URL,
                params={"visibility": self.visibility},
                files={"file": (filename, data, "application/octet-stream")},
                timeout=60,
            )
            if r.status_code == 201:
                body = r.json()
                replay_id = body.get("id", "")
                url = f"https://ballchasing.com/replay/{replay_id}"
                logger.info("Uploaded %s → %s", filename, url)
                return UploadResult(ok=True, replay_id=replay_id, url=url, filename=filename)
            if r.status_code == 409:
                logger.info("Duplicate replay %s — already on ballchasing", filename)
                return UploadResult(ok=True, duplicate=True, filename=filename)
            err = f"HTTP {r.status_code}: {r.text[:200]}"
            logger.error("Upload failed for %s: %s", filename, err)
            return UploadResult(ok=False, error=err, filename=filename)
        except requests.RequestException as exc:
            logger.error("Upload error for %s: %s", filename, exc)
            return UploadResult(ok=False, error=str(exc), filename=filename)

    def upload(self, replay_path: Path) -> UploadResult:
        filename = replay_path.name
        try:
            with open(replay_path, "rb") as fh:
                r = self._session.post(
                    _BC_UPLOAD_URL,
                    params={"visibility": self.visibility},
                    files={"file": (filename, fh, "application/octet-stream")},
                    timeout=60,
                )

            if r.status_code == 201:
                body = r.json()
                replay_id = body.get("id", "")
                url = f"https://ballchasing.com/replay/{replay_id}"
                logger.info("Uploaded %s → %s", filename, url)
                return UploadResult(ok=True, replay_id=replay_id, url=url, filename=filename)

            if r.status_code == 409:
                logger.info("Duplicate replay %s — already on ballchasing", filename)
                return UploadResult(ok=True, duplicate=True, filename=filename)

            err = f"HTTP {r.status_code}: {r.text[:200]}"
            logger.error("Upload failed for %s: %s", filename, err)
            return UploadResult(ok=False, error=err, filename=filename)

        except requests.RequestException as exc:
            logger.error("Upload error for %s: %s", filename, exc)
            return UploadResult(ok=False, error=str(exc), filename=filename)


class RockyClient:
    """Upload replays to Rocky (lexore.ca).

    No authentication required — Rocky is open to all.
    """

    def __init__(self) -> None:
        self._session = requests.Session()

    def upload_bytes(self, filename: str, data: bytes) -> UploadResult:
        """Upload replay from in-memory bytes to Rocky."""
        try:
            r = self._session.post(
                _ROCKY_UPLOAD_URL,
                files={"file": (filename, data, "application/octet-stream")},
                timeout=60,
            )
            if r.status_code == 201:
                logger.info("Rocky: uploaded %s", filename)
                return UploadResult(ok=True, filename=filename)
            if r.status_code == 409:
                logger.info("Rocky: duplicate replay %s — already uploaded", filename)
                return UploadResult(ok=True, duplicate=True, filename=filename)
            err = f"HTTP {r.status_code}: {r.text[:200]}"
            logger.error("Rocky: upload failed for %s: %s", filename, err)
            return UploadResult(ok=False, error=err, filename=filename)
        except requests.RequestException as exc:
            logger.error("Rocky: upload error for %s: %s", filename, exc)
            return UploadResult(ok=False, error=str(exc), filename=filename)


class BallcamClient:
    """Upload replays to BallCam.tv.

    Requires a Personal Access Token (PAT) from https://ballcam.tv.
    """

    _MAX_RETRIES = 3

    def __init__(self, token: str, visibility: str = "public") -> None:
        self.token = token
        self.visibility = visibility
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self._session.headers["User-Agent"] = "hudayUpload/1.0"

    def upload_bytes(self, filename: str, data: bytes, title: str = "") -> UploadResult:
        """Upload replay from in-memory bytes to BallCam.tv (with retries)."""
        form: dict = {"visibility": self.visibility}
        if title:
            form["title"] = title[:100]  # API max 100 chars

        last_exc: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                r = self._session.post(
                    _BALLCAM_UPLOAD_URL,
                    files={"file": (filename, data, "application/octet-stream")},
                    data=form,
                    timeout=60,
                )
                if r.status_code == 201:
                    body = r.json()
                    replay_id = body.get("replay", {}).get("id", "")
                    url = f"https://ballcam.tv/replay/{replay_id}" if replay_id else ""
                    logger.info("BallCam: uploaded %s → %s", filename, url)
                    return UploadResult(ok=True, replay_id=replay_id, url=url, filename=filename)
                err_body = {}
                try:
                    err_body = r.json()
                except Exception:
                    pass
                err = err_body.get("message") or f"HTTP {r.status_code}: {r.text[:200]}"
                logger.error("BallCam: upload failed for %s: %s", filename, err)
                return UploadResult(ok=False, error=err, filename=filename)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self._MAX_RETRIES:
                    wait = 2 ** attempt  # 2s, 4s
                    logger.warning("BallCam: connection error (attempt %d/%d), retrying in %ds: %s",
                                   attempt, self._MAX_RETRIES, wait, exc)
                    time.sleep(wait)
                    # Reset the session to get a fresh connection
                    self._session.close()
                    self._session = requests.Session()
                    self._session.headers["Authorization"] = f"Bearer {self.token}"
                    self._session.headers["User-Agent"] = "hudayUpload/1.0"

        logger.error("BallCam: upload error for %s after %d attempts: %s",
                      filename, self._MAX_RETRIES, last_exc)
        return UploadResult(ok=False, error=str(last_exc), filename=filename)


# ── replay-finding helpers ────────────────────────────────────────────────────


def find_newest_replay(
    demos_dir: Path,
    uploaded: set[str],
    max_age_seconds: float = 120,
) -> Optional[Path]:
    """Return the newest .replay file not yet uploaded, created within max_age_seconds."""
    if not demos_dir.is_dir():
        logger.warning("Replays folder not found: %s", demos_dir)
        return None

    cutoff = time.time() - max_age_seconds
    candidates = [
        p
        for p in demos_dir.glob("*.replay")
        if p.stat().st_mtime >= cutoff and p.name not in uploaded
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_all_unuploaded(demos_dir: Path, uploaded: set[str]) -> list[Path]:
    """Return all .replay files not yet uploaded, newest first."""
    if not demos_dir.is_dir():
        return []
    return sorted(
        (p for p in demos_dir.glob("*.replay") if p.name not in uploaded),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
