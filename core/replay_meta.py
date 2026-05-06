"""Minimal Rocket League .replay header parser.

Reads only the key-value property section at the top of the file and stops
before the frame data, so it is fast and requires no external libraries.

Typical properties extracted:
    Date        "2026-04-28:0-9-0"  (YYYY-MM-DD:H-M-S, no zero-padding)
    PlayerName  "huday"
    PlayerTeam  0 or 1
    Team0Score  3
    Team1Score  1
    Playlist    11  (same IDs as PsyNet — Ranked Doubles, etc.)
    TeamSize    2
    ReplayName  ""  (user-set label, almost always empty)
    Id          "934BAAB6..."
"""
from __future__ import annotations

import logging
import struct
from typing import Any

logger = logging.getLogger(__name__)

# PlaylistID → human-readable name (covers ranked + common casual playlists)
PLAYLIST_NAMES: dict[int, str] = {
    1:  "Casual Duel",
    2:  "Casual Doubles",
    3:  "Casual Standard",
    4:  "Casual Chaos",
    10: "Ranked Duel",
    11: "Ranked Doubles",
    13: "Ranked Standard",
    22: "Tournament",
    27: "Ranked Hoops",
    28: "Ranked Rumble",
    29: "Ranked Dropshot",
    30: "Ranked Snowday",
}


# ── public API ────────────────────────────────────────────────────────────────

def parse_header(data: bytes) -> dict[str, Any]:
    """Return a dict of header properties from raw replay bytes.

    Returns an empty dict on any parse error — callers should treat every key
    as optional and provide fallbacks.
    """
    try:
        return _parse(data)
    except Exception as exc:
        logger.debug("Replay header parse error: %s", exc)
        return {}


def build_title(props: dict[str, Any], fallback_name: str = "") -> str:
    """Build a human-readable replay title from parsed header properties.

    Format: '2026-04-28.00.09 huday Ranked Doubles Win'
    Any component that cannot be determined is simply omitted.
    """
    parts: list[str] = []

    # ── date / time ──────────────────────────────────────────────────────────
    # Stored in the replay as "YYYY-MM-DD HH-MM-SS" (space-separated, zero-padded)
    date_str = props.get("Date", "")
    if date_str and isinstance(date_str, str):
        try:
            date_part, _, time_part = date_str.partition(" ")
            h, m = (time_part.split("-") + ["0", "0"])[:2]
            parts.append(f"{date_part}.{int(h):02d}.{int(m):02d}")
        except Exception:
            pass

    # ── player name ──────────────────────────────────────────────────────────
    # Key is "PlayerName" if injected externally; replay file itself stores it
    # inside the PlayerStats ArrayProperty (not parsed here).
    name = (props.get("PlayerName") or fallback_name or "").strip()
    if name:
        parts.append(name)

    # ── game mode ────────────────────────────────────────────────────────────
    # "Playlist" is not in the replay header; caller should inject it from PsyNet.
    playlist_id = int(props.get("Playlist") or 0)
    mode = PLAYLIST_NAMES.get(playlist_id, "")
    if mode:
        parts.append(mode)

    # ── win / loss / draw ────────────────────────────────────────────────────
    # Use WinningTeam (authoritative) together with PrimaryPlayerTeam.
    player_team  = props.get("PrimaryPlayerTeam")
    winning_team = props.get("WinningTeam")
    if isinstance(player_team, int) and isinstance(winning_team, int):
        parts.append("Win" if player_team == winning_team else "Loss")
    else:
        # Fallback: compare scores
        t0 = int(props.get("Team0Score") or 0)
        t1 = int(props.get("Team1Score") or 0)
        if isinstance(player_team, int) and player_team in (0, 1):
            my_score  = t0 if player_team == 0 else t1
            opp_score = t1 if player_team == 0 else t0
            parts.append(
                "Win"  if my_score > opp_score else
                "Loss" if my_score < opp_score else
                "Draw"
            )

    title = " ".join(parts)
    logger.debug("Built replay title: %r  (props: Date=%r Playlist=%r PlayerTeam=%r scores=%d-%d)",
                 title, props.get("Date"), props.get("Playlist"),
                 props.get("PlayerTeam"), t0, t1)
    return title


# ── parser internals ──────────────────────────────────────────────────────────

def _read_str(data: bytes, pos: int) -> tuple[str, int]:
    """Read a length-prefixed string. Negative length means UTF-16 LE."""
    if pos + 4 > len(data):
        raise ValueError(f"EOF reading string length at offset {pos}")
    length = struct.unpack_from("<i", data, pos)[0]
    pos += 4
    if length == 0:
        return "", pos
    if length < 0:
        byte_len = -length * 2
        if pos + byte_len > len(data):
            raise ValueError("EOF inside UTF-16 string")
        s = data[pos : pos + byte_len].decode("utf-16-le", errors="replace").rstrip("\x00")
        return s, pos + byte_len
    else:
        if pos + length > len(data):
            raise ValueError("EOF inside ASCII string")
        s = data[pos : pos + length].decode("latin-1", errors="replace").rstrip("\x00")
        return s, pos + length


def _parse(data: bytes) -> dict[str, Any]:
    if len(data) < 16:
        return {}

    # Header layout (all little-endian):
    #   0-3   header_size   (not needed)
    #   4-7   CRC
    #   8-11  engine_version
    #  12-15  licensee_version
    engine_ver   = struct.unpack_from("<I", data, 8)[0]
    licensee_ver = struct.unpack_from("<I", data, 12)[0]
    pos = 16

    # Net version is present when engine >= 868 and licensee >= 18
    if engine_ver >= 868 and licensee_ver >= 18:
        pos += 4

    # Type name string (e.g. "TAGame.Replay_Soccar_TA") — skip it
    _, pos = _read_str(data, pos)

    props: dict[str, Any] = {}

    while pos < len(data) - 8:
        key, pos = _read_str(data, pos)
        if not key or key == "None":
            break

        type_name, pos = _read_str(data, pos)

        # 8-byte property sub-header: value_size (u32) + array_index (u32)
        if pos + 8 > len(data):
            break
        value_size = struct.unpack_from("<I", data, pos)[0]
        pos += 8
        val_end = pos + value_size  # fallback jump target for unknown types

        try:
            if type_name == "IntProperty":
                props[key] = struct.unpack_from("<i", data, pos)[0]
                pos += 4

            elif type_name in ("StrProperty", "NameProperty"):
                val, pos = _read_str(data, pos)
                props[key] = val

            elif type_name == "FloatProperty":
                props[key] = struct.unpack_from("<f", data, pos)[0]
                pos += 4

            elif type_name == "BoolProperty":
                # value_size is 0; actual value is a single byte after the sub-header
                props[key] = bool(struct.unpack_from("<B", data, pos)[0])
                pos += 1

            elif type_name == "ByteProperty":
                # Two strings: enum-type key + enum value
                _, pos = _read_str(data, pos)   # e.g. "OnlinePlatform"
                val, pos = _read_str(data, pos) # e.g. "OnlinePlatform_Epic"
                props[key] = val

            elif type_name == "QWordProperty":
                props[key] = struct.unpack_from("<Q", data, pos)[0]
                pos += 8

            else:
                # ArrayProperty or unknown — skip using the declared size
                pos = val_end

        except Exception:
            pos = val_end  # recover and continue

    return props
