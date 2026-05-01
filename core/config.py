"""Config management — stores settings in %APPDATA%\\RLReplayUploader."""
import json
import os
import winreg
from pathlib import Path

APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
CONFIG_DIR = APPDATA / "RLReplayUploader"
CONFIG_FILE = CONFIG_DIR / "config.json"
UPLOADED_FILE = CONFIG_DIR / "uploaded.txt"          # legacy filename store
UPLOADED_GUIDS_FILE = CONFIG_DIR / "uploaded_guids.txt"  # Epic match GUIDs

_DEFAULTS = {
    "ballchasing_token": "",
    "ballchasing_visibility": "public",
    "replays_path": "",
    "rl_install_path": "",
    "stats_api_port": 49123,
    "start_minimized": False,
    "launch_at_startup": False,
    "auto_upload": True,
    "post_game_delay": 30,
    # Epic Games auth (populated after user logs in)
    "epic_refresh_token": "",
    "epic_account_id": "",
    "epic_display_name": "",
}


class Config:
    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._data: dict = dict(_DEFAULTS)
        self._load()
        if not self._data["replays_path"]:
            self._data["replays_path"] = _default_replays_path()
        if not self._data["rl_install_path"]:
            self._data["rl_install_path"] = _detect_rl_install()

    def _load(self) -> None:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                stored = json.load(f)
            self._data.update({k: v for k, v in stored.items() if k in _DEFAULTS})
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            pass

    def save(self) -> None:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    def __getattr__(self, key: str):
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key: str, value) -> None:
        if key.startswith("_"):
            object.__setattr__(self, key, value)
        else:
            self._data[key] = value

    # ── uploaded-replay tracking ─────────────────────────────────────────
    def load_uploaded(self) -> set[str]:
        try:
            with open(UPLOADED_FILE, encoding="utf-8") as f:
                return {line.strip() for line in f if line.strip()}
        except FileNotFoundError:
            return set()

    def add_uploaded(self, name: str) -> None:
        with open(UPLOADED_FILE, "a", encoding="utf-8") as f:
            f.write(name + "\n")

    # ── Epic match GUID tracking ──────────────────────────────────────────
    def load_uploaded_guids(self) -> set[str]:
        try:
            with open(UPLOADED_GUIDS_FILE, encoding="utf-8") as f:
                return {line.strip() for line in f if line.strip()}
        except FileNotFoundError:
            return set()

    def add_uploaded_guid(self, guid: str) -> None:
        with open(UPLOADED_GUIDS_FILE, "a", encoding="utf-8") as f:
            f.write(guid + "\n")

    # ── convenience ──────────────────────────────────────────────────────
    @property
    def has_bc_token(self) -> bool:
        return bool(self._data.get("ballchasing_token", "").strip())

    @property
    def has_epic_auth(self) -> bool:
        return bool(self._data.get("epic_refresh_token", "").strip())

    def ini_path(self) -> Path | None:
        """Derive the ini path from the replays folder — both live under TAGame/.

        replays_path = ...\\TAGame\\Demos
        ini_path     = ...\\TAGame\\Config\\DefaultStatsAPI.ini
        """
        replays = self._data.get("replays_path", "")
        if not replays:
            return None
        demos_dir = Path(replays)          # .../TAGame/Demos
        tagame_dir = demos_dir.parent      # .../TAGame
        return tagame_dir / "Config" / "DefaultStatsAPI.ini"


def _default_replays_path() -> str:
    p = Path.home() / "Documents" / "My Games" / "Rocket League" / "TAGame" / "Demos"
    return str(p)


def _detect_rl_install() -> str:
    # Try Steam registry
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 252950",
        ) as key:
            loc = winreg.QueryValueEx(key, "InstallLocation")[0]
            if loc and Path(loc).exists():
                return loc
    except OSError:
        pass

    # Try well-known paths
    candidates = [
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\rocketleague"),
        Path(r"C:\Program Files\Epic Games\rocketleague"),
        Path(r"C:\Program Files\Epic Games\Rocket League"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)

    # Try Epic manifest folder
    epic_manifests = Path(
        os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    ) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    try:
        for item in epic_manifests.glob("*.item"):
            try:
                with open(item, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("AppName") in ("Sugar", "rocketleague"):
                    loc = data.get("InstallLocation", "")
                    if loc and Path(loc).exists():
                        return loc
            except (json.JSONDecodeError, KeyError):
                pass
    except (FileNotFoundError, PermissionError):
        pass

    return ""
