"""Persistent buffer for replays that could not be uploaded (e.g. ballchasing quota filled).

Each buffered replay is stored on disk as two files under
``%APPDATA%\\RLReplayUploader\\pending_uploads``:

    <match_guid>.replay  — replay bytes (ReplayName already written in)
    <match_guid>.json    — small metadata sidecar (upload filename, timestamps…)

Files survive PC restarts, so pending replays are retried on the next upload
run (startup, Upload Now, RL close, …) until ballchasing accepts them.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PENDING_DIRNAME = "pending_uploads"

# Match GUIDs are hex strings — sanitise anything else so the GUID can safely
# be used as a filename on Windows.
_SAFE_GUID = re.compile(r"[^A-Za-z0-9_-]")


def _safe_guid(guid: str) -> str:
    return _SAFE_GUID.sub("_", guid.strip())


def default_pending_dir() -> Path:
    """Return the on-disk pending buffer directory (created on demand)."""
    # Import lazily to avoid a hard dependency cycle (config imports nothing
    # from here, but keep it decoupled anyway for tests).
    from core.config import CONFIG_DIR

    d = CONFIG_DIR / _PENDING_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


class PendingUploadStore:
    """File-backed FIFO queue of replays waiting for (re-)upload."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = Path(directory) if directory is not None else default_pending_dir()
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── paths ──────────────────────────────────────────────────────────────

    def _replay_path(self, guid: str) -> Path:
        return self._dir / f"{_safe_guid(guid)}.replay"

    def _meta_path(self, guid: str) -> Path:
        return self._dir / f"{_safe_guid(guid)}.json"

    # ── public API ─────────────────────────────────────────────────────────

    def add(
        self,
        guid: str,
        upload_name: str,
        data: bytes,
        display_name: str = "",
        playlist_id: int = 0,
        error: str = "",
    ) -> bool:
        """Buffer (or refresh) a replay. Returns True on success."""
        guid = guid.strip()
        if not guid or not data:
            return False
        meta: dict[str, Any] = {
            "match_guid": guid,
            "upload_name": upload_name or f"{guid}.replay",
            "display_name": display_name,
            "playlist_id": int(playlist_id or 0),
            "timestamp": time.time(),
            "size": len(data),
            "error": error,
        }
        try:
            # Atomic writes: tmp file + rename, so a crash/restart mid-write
            # never leaves a half-written replay behind.
            rp, mp = self._replay_path(guid), self._meta_path(guid)
            tmp_rp = rp.with_suffix(".replay.tmp")
            tmp_mp = mp.with_suffix(".json.tmp")
            tmp_rp.write_bytes(data)
            tmp_mp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            tmp_rp.replace(rp)
            tmp_mp.replace(mp)
            logger.info("Buffered replay %s (%d bytes) — quota/exhausted?", guid, len(data))
            return True
        except Exception as exc:
            logger.warning("Could not buffer replay %s: %s", guid, exc)
            return False

    def contains(self, guid: str) -> bool:
        guid = guid.strip()
        return self._meta_path(guid).exists() and self._replay_path(guid).exists()

    def guids(self) -> set[str]:
        """Return the set of buffered match GUIDs (metadata files present)."""
        out: set[str] = set()
        try:
            for mp in self._dir.glob("*.json"):
                if mp.name.endswith(".tmp"):
                    continue
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                    g = str(meta.get("match_guid", "") or mp.stem).strip()
                    if g and self._replay_path(g).exists():
                        out.add(g)
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def list_pending(self) -> list[dict[str, Any]]:
        """Return pending metadata dicts, oldest-first. Skips corrupt entries."""
        items: list[dict[str, Any]] = []
        try:
            for mp in self._dir.glob("*.json"):
                if mp.name.endswith(".tmp"):
                    continue
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                    guid = str(meta.get("match_guid", "") or mp.stem).strip()
                    if not guid or not self._replay_path(guid).exists():
                        continue
                    meta["match_guid"] = guid
                    items.append(meta)
                except Exception as exc:
                    logger.debug("Skipping corrupt pending entry %s: %s", mp.name, exc)
                    continue
        except Exception as exc:
            logger.debug("Could not list pending uploads: %s", exc)
        items.sort(key=lambda m: float(m.get("timestamp", 0) or 0))
        return items

    def load_data(self, guid: str) -> bytes | None:
        """Return the buffered replay bytes, or None if missing/unreadable."""
        try:
            p = self._replay_path(guid.strip())
            if not p.exists():
                return None
            return p.read_bytes()
        except Exception as exc:
            logger.warning("Could not read buffered replay %s: %s", guid, exc)
            return None

    def remove(self, guid: str) -> None:
        """Drop a buffered replay (after successful upload / duplicate)."""
        guid = guid.strip()
        for p in (self._replay_path(guid), self._meta_path(guid)):
            try:
                if p.exists():
                    p.unlink()
            except Exception as exc:
                logger.debug("Could not delete %s: %s", p, exc)

    def count(self) -> int:
        return len(self.guids())

    # ── maintenance ────────────────────────────────────────────────────────

    def cleanup_orphans(self) -> None:
        """Delete stray .tmp files and sidecars without replay data."""
        try:
            for tmp in list(self._dir.glob("*.tmp")):
                try:
                    # Only remove stale tmp files (>1h old) to avoid racing
                    # an in-progress write.
                    if time.time() - tmp.stat().st_mtime > 3600:
                        tmp.unlink()
                except Exception:
                    pass
            for mp in self._dir.glob("*.json"):
                try:
                    meta = json.loads(mp.read_text(encoding="utf-8"))
                    guid = str(meta.get("match_guid", "") or mp.stem).strip()
                    if not self._replay_path(guid).exists():
                        mp.unlink()
                except Exception:
                    pass
        except Exception:
            pass
