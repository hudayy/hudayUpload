"""Read and write DefaultStatsAPI.ini to enable the Rocket League Stats API.

Confirmed working format (community-verified via RocketLeagueCustomBoostMeter):
  - Location : <RL Install Dir>\\TAGame\\Config\\DefaultStatsAPI.ini
  - Format   : UE4 ini with [TAGame.MatchStatsExporter_TA] section header
  - Keys     : PacketSendRate=60  Port=49123
  - No spaces around '=' (RL's ini parser is strict)
  - Restart RL after writing
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

_SECTION = "[TAGame.MatchStatsExporter_TA]"
_REQUIRED = {
    "PacketSendRate": "60",
    "Port": "49123",
}


def check_stats_api_enabled(ini_path: Path) -> bool:
    """Return True if the ini already has PacketSendRate > 0 and Port=49123."""
    if not ini_path.exists():
        return False
    settings = _read_kv(ini_path)
    try:
        rate_ok = int(settings.get("packetsendrate", "0")) > 0
    except ValueError:
        rate_ok = False
    port_ok = settings.get("port", "") == "49123"
    return rate_ok and port_ok


def enable_stats_api(ini_path: Path) -> None:
    """Write (or update) DefaultStatsAPI.ini with the required settings.

    Uses strict Key=Value format with [TAGame.MatchStatsExporter_TA] section
    header — this is the UE4 subsystem name RL actually checks; any other
    section name is silently ignored.  No spaces around '='.
    """
    ini_path.parent.mkdir(parents=True, exist_ok=True)

    # Collect any existing keys we want to preserve (outside our required ones)
    extra_lines: list[str] = []
    existing_lower: set[str] = set()

    if ini_path.exists():
        in_stats_section = False
        for line in ini_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_stats_section = stripped.lower() in ("[tagame.matchstatsexporter_ta]", "[statsapi]", "[stats api]")
                continue
            if not in_stats_section or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip().lower()
            existing_lower.add(key)
            # We'll overwrite our required keys; keep any unknowns
            if key not in ("packetsendrate", "port"):
                extra_lines.append(stripped)

    lines = [_SECTION]
    for key, val in _REQUIRED.items():
        lines.append(f"{key}={val}")
    lines.extend(extra_lines)

    ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_ini_text(ini_path: Path) -> str:
    """Return raw ini content for display, or an empty string."""
    try:
        return ini_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


# ── internal ──────────────────────────────────────────────────────────────────

def _read_kv(path: Path) -> dict[str, str]:
    """Parse Key=Value lines (inside any section), returning a lowercase-keyed dict."""
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith(("[", ";", "#")) or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        result[key.strip().lower()] = val.strip()
    return result
