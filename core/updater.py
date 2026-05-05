"""Automatic update checker and installer for hudayUpload.

Flow:
  1. check_for_update() hits GitHub releases API in a background thread.
  2. If a newer version exists, the main window shows an update banner.
  3. User clicks "Update" → download_and_install() downloads the new exe to a
     temp file, writes a batch script that swaps the exe after this process
     exits, launches the batch script, then signals the app to quit.

Running from source (non-frozen): update check still runs and notifies, but
the self-replace step is skipped with a clear message.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

VERSION = "1.5.0"

_GITHUB_API = "https://api.github.com/repos/hudayy/hudayUpload/releases/latest"
_EXE_ASSET_NAME = "hudayUpload.exe"


def _parse_version(tag: str) -> tuple[int, ...]:
    """'v1.3.0' or '1.3.0' → (1, 3, 0)"""
    return tuple(int(x) for x in tag.lstrip("v").split(".") if x.isdigit())


def check_for_update() -> Optional[dict]:
    """Return {'version': str, 'download_url': str} if a newer release exists, else None.

    Raises nothing — all errors are logged and return None.
    """
    try:
        resp = requests.get(
            _GITHUB_API,
            headers={"Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.debug("Update check: GitHub API returned HTTP %d", resp.status_code)
            return None

        data = resp.json()
        tag = data.get("tag_name", "")
        if not tag:
            return None

        latest = _parse_version(tag)
        current = _parse_version(VERSION)

        if latest <= current:
            logger.debug("Update check: already up to date (%s)", VERSION)
            return None

        # Find the exe asset
        download_url = ""
        for asset in data.get("assets", []):
            if asset.get("name") == _EXE_ASSET_NAME:
                download_url = asset.get("browser_download_url", "")
                break

        if not download_url:
            logger.warning("Update check: newer version %s found but no exe asset", tag)
            return None

        logger.info("Update available: %s → %s", VERSION, tag.lstrip("v"))
        return {"version": tag.lstrip("v"), "download_url": download_url}

    except Exception as exc:
        logger.debug("Update check failed: %s", exc)
        return None


def download_and_install(
    download_url: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> None:
    """Download the new exe and set up the batch-file swap, then call quit_cb.

    progress_cb(message) is called on the calling thread with status updates.
    Raises RuntimeError if anything goes wrong (caller should show the message).
    """
    frozen = getattr(sys, "frozen", False)
    current_exe = Path(sys.executable).resolve() if frozen else None

    def _progress(msg: str) -> None:
        logger.info("Updater: %s", msg)
        if progress_cb:
            progress_cb(msg)

    _progress("Downloading update…")

    try:
        resp = requests.get(download_url, stream=True, timeout=120)
        if resp.status_code != 200:
            raise RuntimeError(f"Download failed: HTTP {resp.status_code}")

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".exe", prefix="hudayUpload_update_"
        )
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        _progress(f"Downloading… {pct}%")
            tmp.flush()
            new_exe = Path(tmp.name)
        finally:
            tmp.close()

    except requests.RequestException as exc:
        raise RuntimeError(f"Download error: {exc}") from exc

    _progress("Download complete — preparing update…")

    if not frozen:
        # Running from source — can't self-replace, just tell the user
        new_exe.unlink(missing_ok=True)
        raise RuntimeError(
            "Running from source — download succeeded but auto-replace is only "
            "supported for the packaged .exe. Download the new release manually."
        )

    # Write a batch script that:
    #   1. Waits for this process to exit (ping delay)
    #   2. Moves the downloaded exe over the current one
    #   3. Launches the updated exe
    #   4. Deletes itself
    bat_fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="hudayUpload_update_")
    try:
        bat = (
            "@echo off\n"
            "ping 127.0.0.1 -n 4 > nul\n"
            f'move /y "{new_exe}" "{current_exe}"\n'
            f'start "" "{current_exe}"\n'
            'del "%~f0"\n'
        )
        os.write(bat_fd, bat.encode("ascii"))
    finally:
        os.close(bat_fd)

    _progress("Restarting…")

    # Launch the batch script detached so it survives this process exiting
    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
