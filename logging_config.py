"""
Centralized logging configuration for Callisto.

Logs to both console and a persistent file (logs/callisto.log).
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_FILE = os.path.join(LOG_DIR, "callisto.log")
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
LOG_LEVEL = logging.INFO
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 5


class SafeRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that survives Windows file-locking errors.

    On Windows, another process (watchdog, tail, etc.) holding the log file
    open prevents rename during rotation. Standard RotatingFileHandler raises
    PermissionError which cascades into every subsequent log write.

    This subclass catches the error and continues writing to the current file.
    Rotation will succeed on the next attempt once the handle is released.
    """

    def doRollover(self):
        try:
            super().doRollover()
        except (PermissionError, OSError):
            # File is locked by another process — skip rotation this time.
            # The file will grow past maxBytes until the lock is released,
            # at which point the next shouldRollover() triggers a retry.
            pass


def setup_logging(level: int = LOG_LEVEL) -> None:
    """Configure logging for the Callisto system."""
    os.makedirs(LOG_DIR, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Prevent duplicate handlers on repeated calls (e.g., watchdog restart)
    if root.handlers:
        return

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(LOG_FORMAT))

    # Rotating file handler — safe on Windows when watchdog holds a handle
    file_handler = SafeRotatingFileHandler(
        LOG_FILE, maxBytes=MAX_LOG_SIZE, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root.addHandler(console)
    root.addHandler(file_handler)
