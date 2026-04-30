"""Background asyncio client for the Rocket League Stats API TCP socket.

The game hosts a plain-TCP JSON server on localhost:49123 (configurable).
We connect, listen for MatchEnded / ReplayCreated events, and push messages
into a thread-safe queue that the main/GUI thread drains via a poll loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Real event names sent by the RL Stats API (lowercase, game: prefix)
_GAME_END_EVENTS = {"game:match_ended", "game:podium_start"}


class StatsWatcher:
    """Wraps the RL Stats API in a daemon asyncio thread.

    Uses run_forever() + create_task() so the event loop can be stopped
    cleanly without RuntimeError: 'Event loop stopped before Future completed'.
    """

    def __init__(self, port: int = 49123) -> None:
        self.port = port
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._connected = False

        # Thread-safe queue consumed by the GUI poll loop
        self.event_queue: queue.Queue[dict] = queue.Queue()

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="rlstats-loop"
        )
        self._thread.start()

    def stop(self) -> None:
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── thread entry point ────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        # Schedule the reconnect loop as a task so run_forever() manages it.
        self._task = self._loop.create_task(self._run(), name="rlstats-watcher")
        try:
            self._loop.run_forever()
        finally:
            # Cancel the watcher task and wait for it to finish cleanly.
            if self._task and not self._task.done():
                self._task.cancel()
            pending = asyncio.all_tasks(self._loop)
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()

    # ── async reconnect loop ──────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                self._post({"type": "connecting"})
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self.port),
                    timeout=5.0,
                )
            except asyncio.CancelledError:
                return  # task cancelled — exit cleanly
            except (ConnectionRefusedError, OSError, TimeoutError, asyncio.TimeoutError):
                # RL not running yet; wait and retry
                if self._connected:
                    self._connected = False
                    self._post({"type": "disconnected"})
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    return
                continue

            self._connected = True
            self._post({"type": "connected"})
            logger.info("Connected to RL Stats API on port %d", self.port)

            try:
                await self._read_loop(reader)
            except asyncio.CancelledError:
                writer.close()
                return
            except Exception as exc:
                logger.debug("Read loop error: %s", exc)
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

            if self._connected:
                self._connected = False
                self._post({"type": "disconnected"})
                logger.info("Disconnected from RL Stats API")

            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                return

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        decoder = json.JSONDecoder()
        buf = ""
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            buf += chunk.decode("utf-8", errors="ignore")
            while True:
                stripped = buf.lstrip()
                if not stripped:
                    buf = ""
                    break
                if stripped is not buf:
                    buf = stripped
                try:
                    obj, end = decoder.raw_decode(buf)
                except json.JSONDecodeError:
                    break
                buf = buf[end:]
                self._handle_message(obj)

    def _handle_message(self, obj: dict) -> None:
        event = obj.get("Event", "")
        data = obj.get("Data", {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = {}

        logger.info("RL event: %s", event)

        if event in _GAME_END_EVENTS:
            logger.info("Game ended (event=%s)", event)
            self._post({"type": "game_ended", "event": event, "data": data})

    def _post(self, msg: dict) -> None:
        self.event_queue.put_nowait(msg)
