"""
Credential manager — a single, auditable place to read secrets.

WHY THIS EXISTS
---------------
Before this module, sportsbook credentials lived in three hazardous places:
  1. Hard-coded in scraper source (session cookies pasted into Python files)
  2. Ad-hoc os.getenv() calls with inconsistent naming (DK_COOKIE vs
     DRAFTKINGS_SESSION vs CALLISTO_DK_KEY — three names, same secret)
  3. Occasionally just "TODO: read from env" with no implementation

That made it impossible to answer basic operational questions:
  - What secrets does Callisto need to run fully-featured?
  - Which scraper is using which credential?
  - Did we leak a cookie into logs / telegram / the task ledger?

This module fixes all three:
  - One naming convention: ``CALLISTO_<BOOK>_<FIELD>`` (upper-snake).
  - One reader: ``get_credential(book, field)`` — used by every scraper
    and executor. Missing credentials return None (never raise); callers
    decide whether to degrade or fail loudly.
  - Optional OS keychain fallback via the ``keyring`` library — reads only
    if the corresponding env var is absent. We never write to keyring
    from code (Marco manages the keychain manually via the OS UI).
  - ``redact_in_logs()`` — wraps any dict / str before it hits a log
    sink, telegram alert, or task ledger row.

SECRETS ARE NEVER COMMITTED
---------------------------
The repo's .gitignore already covers .env / .env.* files. This module
refuses to read a ``.env`` file from the cwd — it only reads process env
vars (which ``dotenv.load_dotenv`` is expected to have populated at
startup). Hard-coded defaults are deliberately absent.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

logger = logging.getLogger("callisto.credentials")

# ---------------------------------------------------------------------------
# Naming scheme and catalog
# ---------------------------------------------------------------------------
#
# Every secret Callisto could need is declared here. If a caller wants a
# credential whose (book, field) pair is not listed, ``get_credential``
# still returns the env-var value — but the catalog serves as the
# single-source-of-truth for ops / docs / .env.example generation.
#
# NAMING: env var = f"CALLISTO_{book_upper}_{field_upper}"
#   - book slug: lowercase, underscores match book_keys canonical form
#   - field: one of the FIELDS constants below.
#
# We include books Callisto might need even if no scraper uses them yet,
# so adding a new book doesn't require touching ``credentials.py``.

FIELD_USERNAME = "USERNAME"
FIELD_PASSWORD = "PASSWORD"
FIELD_SESSION_COOKIE = "SESSION_COOKIE"
FIELD_API_KEY = "API_KEY"
FIELD_API_SECRET = "API_SECRET"  # For providers that use HMAC-signed requests
FIELD_ACCOUNT_ID = "ACCOUNT_ID"  # Required by some placement flows
FIELD_DEVICE_ID = "DEVICE_ID"    # Some books bind sessions to a device ID
FIELD_TWO_FA_SECRET = "TWO_FA_SECRET"  # TOTP seed for auto-login

ALL_FIELDS: tuple[str, ...] = (
    FIELD_USERNAME,
    FIELD_PASSWORD,
    FIELD_SESSION_COOKIE,
    FIELD_API_KEY,
    FIELD_API_SECRET,
    FIELD_ACCOUNT_ID,
    FIELD_DEVICE_ID,
    FIELD_TWO_FA_SECRET,
)

# Books whose credentials Callisto may use. "Use" ranges from "scraper
# needs an optional session cookie" (Fanatics, DK) to "data provider API
# key" (odds-api.io, the-odds-api.com, action network, brave search).
#
# The book slug here must match `tools.book_keys.canonicalize_book` for
# sportsbooks. Non-sportsbook providers use their own slug.
KNOWN_BOOKS: tuple[str, ...] = (
    # Sportsbooks — scraper session cookies + (future) placement flow
    "draftkings",
    "fanduel",
    "fanatics",
    "betmgm",
    "caesars",
    "pinnacle",
    "betrivers",
    "pointsbet",
    # Data providers — API keys
    "odds_api_io",
    "the_odds_api",
    "action_network",
    "brave_search",
    # Messaging / ops
    "telegram",
)

# Per-book field subset: what fields actually make sense. The catalog is
# purely informational — ``get_credential`` accepts any (book, field)
# pair. Used by list_missing() and the .env.example generator.
BOOK_FIELDS: dict[str, tuple[str, ...]] = {
    "draftkings": (FIELD_USERNAME, FIELD_PASSWORD, FIELD_SESSION_COOKIE, FIELD_ACCOUNT_ID, FIELD_TWO_FA_SECRET),
    "fanduel": (FIELD_USERNAME, FIELD_PASSWORD, FIELD_SESSION_COOKIE, FIELD_ACCOUNT_ID),
    "fanatics": (FIELD_USERNAME, FIELD_PASSWORD, FIELD_SESSION_COOKIE, FIELD_ACCOUNT_ID, FIELD_DEVICE_ID),
    "betmgm": (FIELD_USERNAME, FIELD_PASSWORD, FIELD_SESSION_COOKIE, FIELD_ACCOUNT_ID),
    "caesars": (FIELD_USERNAME, FIELD_PASSWORD, FIELD_SESSION_COOKIE),
    "pinnacle": (FIELD_USERNAME, FIELD_PASSWORD, FIELD_API_KEY),
    "betrivers": (FIELD_USERNAME, FIELD_PASSWORD, FIELD_SESSION_COOKIE),
    "pointsbet": (FIELD_USERNAME, FIELD_PASSWORD, FIELD_SESSION_COOKIE),
    "odds_api_io": (FIELD_API_KEY,),
    "the_odds_api": (FIELD_API_KEY,),
    "action_network": (FIELD_API_KEY, FIELD_SESSION_COOKIE),
    "brave_search": (FIELD_API_KEY,),
    "telegram": (FIELD_API_KEY,),  # bot token lives here
}

# Historical / alternate env var names that existing code expects. We
# check these as a fallback when the canonical CALLISTO_<BOOK>_<FIELD>
# name is unset, so this refactor does not silently break the running
# process. New code MUST use the canonical names.
_LEGACY_ENV_ALIASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("odds_api_io", FIELD_API_KEY): ("ODDS_API_IO_KEY", "ODDSAPI_IO_KEY"),
    ("the_odds_api", FIELD_API_KEY): ("ODDS_API_KEY", "THE_ODDS_API_KEY"),
    ("brave_search", FIELD_API_KEY): ("BRAVE_API_KEY",),
    ("telegram", FIELD_API_KEY): ("TELEGRAM_BOT_TOKEN",),
    ("action_network", FIELD_API_KEY): ("ACTION_NETWORK_KEY",),
}


# ---------------------------------------------------------------------------
# Keychain (optional)
# ---------------------------------------------------------------------------
#
# The `keyring` package abstracts over Windows Credential Manager, macOS
# Keychain, and Linux Secret Service. We never force-install it — if it's
# not on sys.path we silently skip keychain lookups.

_KEYRING_SERVICE = "callisto"  # Namespace for keychain entries
_keyring_disabled = os.getenv("CALLISTO_DISABLE_KEYRING", "0") == "1"

try:
    if _keyring_disabled:
        _keyring: Any = None
    else:
        import keyring as _keyring  # type: ignore[import-not-found]
except ImportError:
    _keyring = None


def _keyring_get(env_name: str) -> Optional[str]:
    """Read a secret from the OS keychain. Returns None if keyring is
    unavailable, disabled, or the entry does not exist.

    Keyring entries are stored under service='callisto', username=env_name
    so a single keychain lookup matches whatever env var name the caller
    would have used. Marco can add entries via:
        keyring set callisto CALLISTO_FANATICS_SESSION_COOKIE
    """
    if _keyring is None:
        return None
    try:
        return _keyring.get_password(_KEYRING_SERVICE, env_name)
    except Exception as e:
        # keyring can raise on backend errors (locked keychain, missing
        # DBus). Don't let the CLI hang — treat as "no credential".
        logger.debug(f"keyring lookup failed for {env_name}: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def env_var_name(book: str, field: str) -> str:
    """Return the canonical env-var name for a (book, field) pair.

    Callers that want to emit diagnostics pointing users at the env var
    to set should use this helper so the names stay consistent.
    """
    if not book or not field:
        raise ValueError("book and field must be non-empty")
    return f"CALLISTO_{book.upper()}_{field.upper()}"


def get_credential(book: str, field: str) -> Optional[str]:
    """Read a single credential. Returns None if unset everywhere.

    Lookup order:
      1. Canonical env var CALLISTO_<BOOK>_<FIELD>
      2. Legacy env var aliases (compatibility with pre-refactor code)
      3. OS keychain entry under service='callisto', username=<canonical>

    This function NEVER raises for missing credentials — the caller
    decides whether to degrade (fall back to unauthenticated scraping)
    or fail loudly (placement flow refuses to run without credentials).
    """
    book_norm = (book or "").lower().strip()
    field_norm = (field or "").upper().strip()
    if not book_norm or not field_norm:
        return None

    canonical = env_var_name(book_norm, field_norm)

    # 1. Canonical env var
    val = os.environ.get(canonical)
    if val:
        return val

    # 2. Legacy aliases
    aliases = _LEGACY_ENV_ALIASES.get((book_norm, field_norm), ())
    for alias in aliases:
        val = os.environ.get(alias)
        if val:
            return val

    # 3. OS keychain fallback
    val = _keyring_get(canonical)
    if val:
        return val

    return None


def has_credential(book: str, field: str) -> bool:
    """Convenience predicate — True if get_credential returns a truthy value."""
    return bool(get_credential(book, field))


def list_missing(
    books: Optional[Iterable[str]] = None,
    *,
    required_fields: Optional[dict[str, tuple[str, ...]]] = None,
) -> list[str]:
    """Return a list of canonical env-var names for credentials that are
    declared in BOOK_FIELDS (or an override map) but currently unset.

    Useful for a startup diagnostic: "Fanatics placement disabled —
    missing CALLISTO_FANATICS_SESSION_COOKIE".
    """
    target = required_fields if required_fields is not None else BOOK_FIELDS
    iter_books = list(books) if books is not None else list(target.keys())
    missing: list[str] = []
    for b in iter_books:
        for f in target.get(b, ()):
            if not has_credential(b, f):
                missing.append(env_var_name(b, f))
    return missing


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
#
# The classic log-safety bug: session cookies / API keys end up inside an
# exception message that gets logged, or inside a dict dumped to the task
# ledger. redact_in_logs wraps any value before it crosses a log boundary.
#
# We redact by both:
#   - key name match (any KEY containing a sensitive substring)
#   - value match (any STRING that matches a known live credential)
#
# Value-matching catches cases where the credential got embedded in a
# URL or error message ("auth failed for cookie=abc123def...").

_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "pwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "cookie",
    "session",
    "auth",
    "bearer",
    "credential",
    "otp",
    "two_fa",
    "2fa",
)

# Substrings that should NOT trigger redaction even though they match
# the naive list above. Eg. "auth_status" is just a label.
_SAFE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "auth_status",
    "auth_method",
)

_REDACTED = "***"


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    if any(safe in k for safe in _SAFE_KEY_SUBSTRINGS):
        return False
    return any(s in k for s in _SENSITIVE_KEY_SUBSTRINGS)


def _collect_live_credentials() -> list[str]:
    """Grab every currently-set credential value. Used for value-based
    redaction in strings. We only pull from the declared BOOK_FIELDS
    catalog — bespoke env vars outside the catalog are not scanned.
    """
    vals: list[str] = []
    for book, fields in BOOK_FIELDS.items():
        for field in fields:
            v = get_credential(book, field)
            if v and len(v) >= 6:
                # Ignore very short values — redacting "a1" would nuke
                # half the log file.
                vals.append(v)
    return vals


def redact_in_logs(obj: Any) -> Any:
    """Return a copy of obj with credential-bearing values replaced.

    Supported inputs:
      - str: returns a string with any live credential substring replaced.
      - dict: recursively redacts; keys matching a sensitive name get
              their value replaced wholesale.
      - list/tuple: recurses into each element.
      - Any other type: returned unchanged.

    Never mutates the input.
    """
    creds = _collect_live_credentials()

    def _redact_str(s: str) -> str:
        out = s
        for c in creds:
            if c and c in out:
                out = out.replace(c, _REDACTED)
        return out

    def _walk(o: Any) -> Any:
        if isinstance(o, str):
            return _redact_str(o)
        if isinstance(o, dict):
            result: dict = {}
            for k, v in o.items():
                if isinstance(k, str) and _is_sensitive_key(k):
                    result[k] = _REDACTED
                else:
                    result[k] = _walk(v)
            return result
        if isinstance(o, list):
            return [_walk(x) for x in o]
        if isinstance(o, tuple):
            return tuple(_walk(x) for x in o)
        return o

    return _walk(obj)


__all__ = [
    "FIELD_USERNAME",
    "FIELD_PASSWORD",
    "FIELD_SESSION_COOKIE",
    "FIELD_API_KEY",
    "FIELD_API_SECRET",
    "FIELD_ACCOUNT_ID",
    "FIELD_DEVICE_ID",
    "FIELD_TWO_FA_SECRET",
    "ALL_FIELDS",
    "KNOWN_BOOKS",
    "BOOK_FIELDS",
    "env_var_name",
    "get_credential",
    "has_credential",
    "list_missing",
    "redact_in_logs",
]
