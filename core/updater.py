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

VERSION = "1.7.3"

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

    # Write a VBScript launcher and a batch script.
    #
    # Why split it this way: when the batch tries to run the new exe directly
    # (via `start`, PowerShell, or cmd), the PyInstaller bootloader fails with
    # "Failed to load Python DLL" because Windows Defender's real-time scanner
    # is still inspecting the freshly-written exe and the _MEI{xxx}\python*.dll
    # it just extracted. A long timeout helps but isn't always enough. By
    # delegating the launch to wscript.exe via a .vbs file, the new process
    # starts in a completely fresh process tree (parent = explorer/wscript)
    # with its own scan window — far more reliable than chaining from cmd.
    #
    # Flow:
    #   batch:   wait → move (retry if locked) → wait (AV settle) → run vbs → self-delete
    #   vbs:     short pause → ShellExecute the new exe → self-delete
    exe_dir = str(current_exe.parent)

    vbs_fd, vbs_path = tempfile.mkstemp(suffix=".vbs", prefix="hudayUpload_launch_")
    try:
        # ShellExecute (vbActivate=1) launches with a clean detached context.
        # WScript.Sleep gives Defender extra time even if the batch's wait was
        # too short. The script then deletes itself.
        # Single-quoted Python strings → escape backslashes for VBS strings.
        exe_str = str(current_exe).replace('"', '""')
        dir_str = exe_dir.replace('"', '""')
        vbs = (
            'Set sh = CreateObject("WScript.Shell")\r\n'
            'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
            'WScript.Sleep 5000\r\n'
            f'sh.CurrentDirectory = "{dir_str}"\r\n'
            f'sh.Run """{exe_str}""", 1, False\r\n'
            'WScript.Sleep 1000\r\n'
            'fso.DeleteFile WScript.ScriptFullName, True\r\n'
        )
        # VBS works fine as ASCII for our paths; if the user has non-ASCII
        # paths we'd need utf-16-le with BOM, but that's vanishingly rare.
        os.write(vbs_fd, vbs.encode("utf-8"))
    finally:
        os.close(vbs_fd)

    bat_fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="hudayUpload_update_")
    try:
        bat = (
            "@echo off\n"
            # Wait for the parent (this) process to fully exit
            "timeout /t 3 /nobreak > nul\n"
            ":retry_move\n"
            f'move /y "{new_exe}" "{current_exe}" > nul 2>&1\n'
            "if errorlevel 1 (\n"
            "    timeout /t 1 /nobreak > nul\n"
            "    goto retry_move\n"
            ")\n"
            # Give Defender real-time protection time to scan the new exe.
            # Combined with the 5-second sleep inside the .vbs, this gives a
            # generous ~13 seconds total before the new process is launched.
            "timeout /t 8 /nobreak > nul\n"
            # Hand off to wscript so the new exe gets a clean process tree.
            # /B keeps the batch quiet; wscript itself detaches.
            f'start "" /B wscript.exe "{vbs_path}"\n'
            '(goto) 2>nul & del "%~f0"\n'
        )
        os.write(bat_fd, bat.encode("ascii"))
    finally:
        os.close(bat_fd)

    _progress("Restarting…")

    # CREATE_NO_WINDOW keeps full env inheritance (DETACHED_PROCESS strips PATH,
    # which breaks PyInstaller's DLL loading on relaunch).
    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
