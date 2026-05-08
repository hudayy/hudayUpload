"""Minimal Rocket League .replay header parser + writer.

Reads only the key-value property section at the top of the file and stops
before the frame data, so it is fast and requires no external libraries.

Typical properties extracted:
    Date               "2026-04-28 00-09-00"  (YYYY-MM-DD HH-MM-SS, zero-padded)
    PrimaryPlayerTeam  0 or 1  (which team the recording player is on)
    WinningTeam        0 or 1
    Team0Score         3
    Team1Score         1
    Playlist           11  (same IDs as PsyNet — Ranked Doubles, etc.)
    TeamSize           2
    ReplayName         ""  (user-set label, almost always empty on fresh replays)
    MatchGUID          "934BAAB6..."
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
    6:  "Private",
    10: "Ranked Duel",
    11: "Ranked Doubles",
    13: "Ranked Standard",
    22: "Tournament",
    27: "Ranked Hoops",
    28: "Ranked Rumble",
    29: "Ranked Dropshot",
    30: "Ranked Snowday",
    34: "Heatseeker",
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
    name = (props.get("PlayerName") or fallback_name or "").strip()
    if name:
        parts.append(name)

    # ── game mode ────────────────────────────────────────────────────────────
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
    logger.debug(
        "Built replay title: %r  (Date=%r Playlist=%r PrimaryPlayerTeam=%r WinningTeam=%r)",
        title, props.get("Date"), props.get("Playlist"),
        props.get("PrimaryPlayerTeam"), props.get("WinningTeam"),
    )
    return title


def write_replay_name(data: bytes, name: str) -> bytes:
    """Return a copy of the replay bytes with the ReplayName property set to *name*.

    Locates the ReplayName StrProperty in the header, replaces its string value,
    then recalculates header_size (bytes 0-3) and the CRC (bytes 4-7) so the
    file remains valid. Returns the original bytes unchanged on any error.
    """
    try:
        return _inject_replay_name(data, name)
    except Exception as exc:
        logger.warning("Could not write ReplayName to replay binary: %s", exc)
        return data


# ── CRC ───────────────────────────────────────────────────────────────────────

def _crc32_rl(body: bytes) -> int:
    """CRC32 variant used by Rocket League replay files.

    Polynomial 0x04C11DB7, init 0x10340DFE, XorOut 0xFFFFFFFF,
    no input/output reflection.  Verified against real replay files.
    """
    crc = 0x10340DFE
    for byte in body:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if (crc & 0x80000000) else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc ^ 0xFFFFFFFF


# ── write internals ───────────────────────────────────────────────────────────

def _prop_str(s: str) -> bytes:
    """Encode a property key/type as a length-prefixed null-terminated ASCII string."""
    enc = s.encode("latin-1") + b"\x00"
    return struct.pack("<i", len(enc)) + enc


def _inject_replay_name(data: bytes, name: str) -> bytes:
    engine_ver   = struct.unpack_from("<I", data, 8)[0]
    licensee_ver = struct.unpack_from("<I", data, 12)[0]
    pos = 16
    if engine_ver >= 868 and licensee_ver >= 18:
        pos += 4

    _, pos = _read_str(data, pos)  # type name string

    none_pos: int = -1  # byte offset of the "None" terminator key, if we reach it

    while pos < len(data) - 8:
        key_start = pos
        try:
            key, pos = _read_str(data, pos)
        except Exception:
            break
        if not key or key == "None":
            none_pos = key_start
            break

        try:
            type_name, pos = _read_str(data, pos)
        except Exception:
            break

        if pos + 8 > len(data):
            break
        value_size_pos = pos
        value_size = struct.unpack_from("<I", data, pos)[0]
        pos += 8
        val_end = pos + value_size

        if key == "ReplayName" and type_name == "StrProperty":
            # ── UPDATE existing property ──────────────────────────────────────
            old_str_start = pos
            old_str_end   = val_end

            encoded       = name.encode("utf-8") + b"\x00"
            new_str_bytes = struct.pack("<i", len(encoded)) + encoded
            delta         = len(new_str_bytes) - (old_str_end - old_str_start)

            new_data = bytearray(data[:old_str_start] + new_str_bytes + data[old_str_end:])
            struct.pack_into("<I", new_data, value_size_pos, len(new_str_bytes))
            old_header_size = struct.unpack_from("<I", new_data, 0)[0]
            new_header_size = old_header_size + delta
            struct.pack_into("<I", new_data, 0, new_header_size)
            new_crc = _crc32_rl(bytes(new_data[8 : 8 + new_header_size]))
            struct.pack_into("<I", new_data, 4, new_crc)

            logger.info("ReplayName updated in replay binary (%+d bytes): %r", delta, name)
            return bytes(new_data)

        # Advance pos past this property's value — must mirror _parse() logic
        # because BoolProperty stores its 1-byte value outside of value_size.
        try:
            if type_name == "BoolProperty":
                pos = val_end + 1
            elif type_name in ("StrProperty", "NameProperty"):
                _, pos = _read_str(data, pos)
            elif type_name == "ByteProperty":
                _, pos = _read_str(data, pos)
                _, pos = _read_str(data, pos)
            else:
                pos = val_end
        except Exception:
            pos = val_end

    # ── INSERT new property before "None" terminator ──────────────────────────
    # Fresh replays that were never named in-game simply omit ReplayName.
    if none_pos < 0:
        logger.warning("ReplayName not found and 'None' terminator missing — file unchanged")
        return data

    encoded     = name.encode("utf-8") + b"\x00"
    value_size  = len(encoded) + 4                  # 4-byte length prefix + string bytes
    new_prop    = (
        _prop_str("ReplayName")                     # key
        + _prop_str("StrProperty")                  # type
        + struct.pack("<II", value_size, 0)          # value_size + index (always 0)
        + struct.pack("<i", len(encoded)) + encoded  # the string value
    )

    new_data = bytearray(data[:none_pos] + new_prop + data[none_pos:])

    old_header_size = struct.unpack_from("<I", new_data, 0)[0]
    new_header_size = old_header_size + len(new_prop)
    struct.pack_into("<I", new_data, 0, new_header_size)
    new_crc = _crc32_rl(bytes(new_data[8 : 8 + new_header_size]))
    struct.pack_into("<I", new_data, 4, new_crc)

    logger.info("ReplayName inserted into replay binary (+%d bytes): %r", len(new_prop), name)
    return bytes(new_data)


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

    engine_ver   = struct.unpack_from("<I", data, 8)[0]
    licensee_ver = struct.unpack_from("<I", data, 12)[0]
    pos = 16

    if engine_ver >= 868 and licensee_ver >= 18:
        pos += 4

    _, pos = _read_str(data, pos)  # type name string

    props: dict[str, Any] = {}

    while pos < len(data) - 8:
        key, pos = _read_str(data, pos)
        if not key or key == "None":
            break

        type_name, pos = _read_str(data, pos)

        if pos + 8 > len(data):
            break
        value_size = struct.unpack_from("<I", data, pos)[0]
        pos += 8
        val_end = pos + value_size

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
                props[key] = bool(struct.unpack_from("<B", data, pos)[0])
                pos += 1

            elif type_name == "ByteProperty":
                _, pos   = _read_str(data, pos)
                val, pos = _read_str(data, pos)
                props[key] = val

            elif type_name == "QWordProperty":
                props[key] = struct.unpack_from("<Q", data, pos)[0]
                pos += 8

            else:
                pos = val_end

        except Exception:
            pos = val_end

    return props
