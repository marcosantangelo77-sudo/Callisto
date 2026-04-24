"""
Centralized logging configuration for Callisto.

Logs to both console and a persistent file (logs/callisto.log).
"""

import logging
import os
import re
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_FILE = os.path.join(LOG_DIR, "callisto.log")
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
LOG_LEVEL = logging.INFO
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 5


_REDACTED_MARKER = "<redacted>"

# Label=value patterns: the VALUE group (group 2) is replaced with the marker.
# We require at least 16 credential-shaped chars to avoid false positives on
# "token=1" or "apiKey=foo" dev scaffolding strings.
_LABEL_VALUE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(
        r"(?i)(apikey|api_key|api-key|secret|token|authorization|auth_token|password|session_cookie|cookie|bearer)"
        r"(\s*[:=]\s*|\s+)"
        r"([A-Za-z0-9_\-./+=]{16,})"
    ),
)


def _mask_label_values(text: str) -> str:
    def _sub(m: re.Match) -> str:
        return f"{m.group(1)}{m.group(2)}{_REDACTED_MARKER}"

    out = text
    for pat in _LABEL_VALUE_PATTERNS:
        out = pat.sub(_sub, out)
    return out


class RedactionFilter(logging.Filter):
    """Scrub api-key / token / password / cookie values from log records.

    Applied at the root logger so every sink (console, rotating file)
    benefits automatically. Two layers:

      1. Regex masking of ``label=value`` / ``Bearer <value>`` shapes where
         the value is at least 16 credential-shaped chars.
      2. Live-credential value replacement via
         ``tools.credentials.redact_in_logs`` so known secrets are masked
         even when they appear bare (eg, embedded in a JSON blob).

    False-positive tradeoff: we only redact when a sensitive LABEL is
    adjacent so ordinary long strings (SQL errors, URLs without keys)
    pass through unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Logging filters must NEVER raise — degrade silently if anything
        # blows up (bad Unicode in record.args, circular tools import, etc).
        try:
            if isinstance(record.msg, str):
                record.msg = self._scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: self._scrub(v) for k, v in record.args.items()}
                else:
                    record.args = tuple(self._scrub(a) for a in record.args)
        except Exception:
            pass
        return True

    @staticmethod
    def _scrub(value):
        if not isinstance(value, str):
            return value
        s = _mask_label_values(value)
        try:
            from tools.credentials import redact_in_logs as _r
            s = _r(s)
        except Exception:
            pass
        return s


def install_redaction_filter(logger: logging.Logger | None = None) -> RedactionFilter:
    """Attach the RedactionFilter to the given logger (root by default).

    Idempotent: will not install a second copy if one is already present.
    Returns the filter instance (useful for tests).
    """
    target = logger if logger is not None else logging.getLogger()
    for existing in target.filters:
        if isinstance(existing, RedactionFilter):
            return existing
    flt = RedactionFilter()
    target.addFilter(flt)
    return flt


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

    # SECURITY (audit 2026-04-23): install credential-redaction filter on the
    # root logger so every sink (console + rotating file) scrubs api-key /
    # token / password / cookie shaped values. See RedactionFilter above.
    install_redaction_filter(root)

    # SECURITY (audit C-1 2026-04-18): silence loggers that print full URLs.
    # httpx INFO logs every request URL — and odds-api.io passes apiKey as a
    # query string, so INFO leaks the live API key into both console and rotating
    # file logs (which sync to OneDrive). Silencing avoids the recurring leak.
    # (oddspapi removed entirely 2026-04-18.)
    for noisy in ("httpx", "httpcore", "urllib3", "websockets", "openai._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
