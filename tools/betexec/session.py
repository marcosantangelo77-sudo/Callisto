"""tools.betexec.session — browser session orchestration for the executor.

Slice-5 split (2026-08): the executor's *instance-level* session methods
(``launch_browser``, ``ensure_logged_in`` with its legacy short-circuit,
``navigate_to_game``, ``place_bet_on_slip``) moved here as free functions
that receive the executor explicitly. The actual Playwright work stays in
``tools.betexec.browser`` / ``tools.betexec.slip``; this module only binds
the executor's live attributes (``_page``, ``_logged_in``, ...) to it.

No Playwright import happens here — tests drive everything with fake pages
and monkeypatched module functions. Nothing arms the executor.
"""

from __future__ import annotations

import logging

from tools.betexec.config import SESSION_DIR
from tools.betexec import browser as betexec_browser
from tools.betexec import slip as betexec_slip

logger = logging.getLogger("callisto.executor")


async def launch_browser(executor) -> None:
    """Launch the persistent Playwright session and bind (context, page)."""
    executor._context, executor._page = await betexec_browser.launch_persistent_session(
        SESSION_DIR
    )
    executor._browser = executor._context


async def ensure_logged_in(executor) -> bool:
    """Check the DK session; prompt for manual login when needed.

    The first time, the browser opens visible so Marco can log in manually.
    After that, cookies persist in SESSION_DIR.
    """
    if not executor._page:
        await launch_browser(executor)

    ok = await betexec_browser.check_logged_in(executor._page)
    executor._logged_in = ok
    return ok


async def navigate_to_game(executor, sport: str, team: str) -> bool:
    """Navigate to a specific game on DraftKings."""
    return await betexec_browser.navigate_to_game(executor._page, sport, team)


async def place_bet_on_slip(
    executor,
    selection_text: str,
    stake: float,
) -> dict:
    """Find a selection, add to slip, enter stake, and confirm.

    Returns dict with success status, screenshot path, and confirmation details.
    """
    return await betexec_slip.place_bet_on_slip(
        executor._page, selection_text, stake
    )
