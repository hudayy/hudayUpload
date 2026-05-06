"""Entry point for hudayUpload."""
import logging
import sys
import tkinter as tk

from core.updater import VERSION  # re-exported so other modules can import from here
from core.log_buffer import log_buffer  # noqa: F401 — imported so the buffer is attached early

_LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

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
