"""Shared in-memory log ring-buffer.

Imported by main.py (to attach to the root logger) and by the settings dialog
(to read records for export). Keeping it in its own module avoids the
re-import problem that occurs when settings_dialog does `import main`
while main.py is loaded as __main__.
"""
from __future__ import annotations

import collections
import logging


class _BufferHandler(logging.Handler):
    def __init__(self, maxlen: int = 2000) -> None:
        super().__init__()
        self.records: collections.deque[str] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            self.handleError(record)


log_buffer = _BufferHandler()
log_buffer.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
