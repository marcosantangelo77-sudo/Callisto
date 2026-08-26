"""tools.betexec.bootstrap — executor database + directory lifecycle.

Slice-5 split (2026-08): ``BetExecutor.initialize`` and ``BetExecutor.shutdown``
moved here as free functions that take the executor instance explicitly. The
facade keeps one-line adapters so the public surface (and every legacy
caller/tests that touch ``ex.initialize()`` / ``ex.shutdown()``) is unchanged.

Responsibilities owned here:
  - open the aiosqlite connection, tag it for the single-writer
    WriteCoordinator routing, set the busy timeout;
  - make sure SCREENSHOT_DIR / SESSION_DIR exist;
  - create the executor_log audit schema (via tools.betexec.logging);
  - tear everything down again on shutdown, resetting the session state and
    leaving the executor DISARMED (``_enabled = False``).

SAFETY: nothing here enables the executor or arms live betting. After a
shutdown the executor is always disabled; initialization never flips any
enablement flag.
"""

from __future__ import annotations

import logging

from tools.betexec.config import DB_PATH, SCREENSHOT_DIR, SESSION_DIR
from tools.betexec import logging as betexec_logging

logger = logging.getLogger("callisto.executor")


async def open_database():
    """Open the executor DB connection, tagged for WriteCoordinator routing."""
    import aiosqlite

    db = await aiosqlite.connect(DB_PATH)
    # Tag for WriteCoordinator routing (single-writer pattern).
    from tools.db_writer import tag_connection as _tag
    _tag(db, DB_PATH)
    await db.execute("PRAGMA busy_timeout = 60000")
    return db


def ensure_directories() -> None:
    """Ensure screenshot + browser-session directories exist."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


async def initialize(executor) -> None:
    """Full ``BetExecutor.initialize`` flow — bind DB, dirs, schema.

    Does NOT touch ``executor._enabled``: initialization never arms the
    executor.
    """
    executor._db = await open_database()
    ensure_directories()
    await betexec_logging.ensure_executor_log_schema(executor._db)
    logger.info("Bet executor initialized")


async def shutdown(executor) -> None:
    """Close the browser context and DB handle; leave the executor disarmed."""
    if executor._browser:
        await executor._browser.close()
        executor._browser = None
        executor._context = None
        executor._page = None
    if executor._db:
        await executor._db.close()
        executor._db = None
    executor._enabled = False
    executor._logged_in = False
    logger.info("Bet executor shut down")
