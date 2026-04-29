"""Watch the Rocket League Demos folder for new .replay files.

Uses watchdog (OS-level ReadDirectoryChangesW on Windows) so detection is
nearly instant — no polling delay. Falls back gracefully if watchdog isn't
installed (app still works via the 5-minute background scan in app.py).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ReplayWatcher:
    def __init__(
        self,
        demos_dir: str,
        on_new_replay: Callable[[Path], None],
    ) -> None:
        self.demos_dir = Path(demos_dir)
        self.on_new_replay = on_new_replay
        self._observer = None
        self._running = False
        self._error: str = ""

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self.demos_dir.exists():
            self._error = f"Demos folder not found:\n{self.demos_dir}"
            logger.warning(self._error)
            return

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_created(self, event: FileCreatedEvent) -> None:
                    if not event.is_directory and str(event.src_path).endswith(".replay"):
                        logger.info("New replay file detected: %s", event.src_path)
                        watcher.on_new_replay(Path(event.src_path))

                def on_moved(self, event: FileMovedEvent) -> None:
                    # RL sometimes writes a temp file then renames it to .replay
                    if not event.is_directory and str(event.dest_path).endswith(".replay"):
                        logger.info("New replay (renamed): %s", event.dest_path)
                        watcher.on_new_replay(Path(event.dest_path))

            self._observer = Observer()
            self._observer.schedule(_Handler(), str(self.demos_dir), recursive=False)
            self._observer.start()
            self._running = True
            logger.info("Watching for replays in: %s", self.demos_dir)

        except ImportError:
            self._error = "watchdog not installed (pip install watchdog)"
            logger.warning(self._error)

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def error(self) -> str:
        return self._error
