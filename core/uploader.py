"""Find the newest replay and upload it to ballchasing.com."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BC_UPLOAD_URL = "https://ballchasing.com/api/v2/upload"
_BC_PING_URL = "https://ballchasing.com/api/"


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

    def upload_bytes(self, filename: str, data: bytes, title: str = "") -> UploadResult:
        """Upload replay from in-memory bytes (downloaded from Epic API)."""
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
                if title and replay_id:
                    import threading
                    threading.Thread(
                        target=self._patch_title,
                        args=(replay_id, title),
                        daemon=True,
                        name=f"patch-title-{replay_id[:8]}",
                    ).start()
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

    def _patch_title(self, replay_id: str, title: str) -> None:
        """PATCH the replay title on ballchasing (runs in a background thread).

        Ballchasing processes replays asynchronously — the record may not exist
        immediately after the 201 upload response.  Retry with increasing delays.
        """
        url = f"https://ballchasing.com/api/v2/replays/{replay_id}"
        # Waits: 5s, 15s, 30s, 60s, 60s  (up to ~3 minutes total)
        for attempt, wait in enumerate([5, 15, 30, 60, 60], start=1):
            time.sleep(wait)
            try:
                r = self._session.patch(
                    url,
                    json={"title": title},
                    headers={"Content-Type": "application/json"},
                    timeout=15,
                )
                if r.status_code == 204:
                    logger.info("Title set (attempt %d): %r → %s", attempt, title, replay_id)
                    return
                logger.warning(
                    "Title PATCH HTTP %d on attempt %d for %s — body: %s",
                    r.status_code, attempt, replay_id, r.text[:300],
                )
                if r.status_code == 404:
                    continue  # replay still processing — try again
                return  # any other error is unlikely to fix itself
            except Exception as exc:
                logger.warning("Title PATCH error on attempt %d for %s: %s", attempt, replay_id, exc)
        logger.warning("Title PATCH gave up after 5 attempts for %s", replay_id)

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
