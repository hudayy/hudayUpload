"""Entry point for hudayUpload."""
import collections
import logging
import sys
import tkinter as tk

from core.updater import VERSION  # re-exported so other modules can import from here

_LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# In-memory ring buffer — stores up to 2000 formatted log lines
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
log_buffer.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FMT))

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    datefmt=_LOG_DATE_FMT,
)
logging.getLogger().addHandler(log_buffer)

from app import Application


def main() -> None:
    root = tk.Tk()
    # Hide the window immediately — app.start() will decide whether to show it
    root.withdraw()

    app = Application(root)
    app.start()

    # If not starting minimized, show the window now
    if not app.config.start_minimized:
        app._win.show()

    root.mainloop()


if __name__ == "__main__":
    main()
