"""Browser-session orchestration for the DraftKings executor (slice 2 split).

Extracted from ``tools/bet_executor.py`` so the Playwright navigation/login
flow is a real, importable module. All functions take an explicit ``page``
(or return one) — no module-level state, no network at import time, no
arming of the executor. Tests drive these with fake ``page`` objects.
"""

import logging
from typing import Optional

from tools.betexec.dk_constants import DK_BASE_URL, DK_SPORT_SLUGS

logger = logging.getLogger("callisto.executor")

# Selectors used to detect an active DraftKings session.
LOGGED_IN_SELECTORS = (
    "[data-testid='user-balance'], "
    ".dk-user-balance, "
    ".sportsbook-header__balance"
)
SIGN_IN_SELECTORS = (
    "[data-testid='sign-in-button'], "
    ".sportsbook-header__sign-in, "
    "a[href*='login']"
)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]
VIEWPORT = {"width": 1280, "height": 900}


async def launch_persistent_session(session_dir):
    """Launch a persistent-context Chromium session.

    Returns ``(context, page)``. Import of playwright is deferred so the
    package stays importable without playwright installed.
    """
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(session_dir),
        headless=False,  # Visible for initial login, can switch to True later
        viewport=VIEWPORT,
        args=LAUNCH_ARGS,
    )
    page = context.pages[0] if context.pages else await context.new_page()
    logger.info("Browser launched with persistent session")
    return context, page


async def check_logged_in(page) -> bool:
    """Return True when the DK homepage shows an active session.

    Pure page-inspection: raises nothing, returns False on any failure.
    """
    try:
        await page.goto(DK_BASE_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        if await page.query_selector(LOGGED_IN_SELECTORS):
            logger.info("DraftKings session active — logged in")
            return True

        sign_in = await page.query_selector(SIGN_IN_SELECTORS)
        if not sign_in:
            # No sign-in button visible — might be logged in with different UI
            return True

        logger.warning(
            "DraftKings login required — browser is open, please log in manually. "
            "Session will persist after first login."
        )
        return False
    except Exception as e:
        logger.error(f"Login check failed: {e}")
        return False


def game_page_url(sport: str) -> Optional[str]:
    """League-page URL for a supported sport slug, else None."""
    slug = DK_SPORT_SLUGS.get(sport, "")
    if not slug:
        return None
    return f"{DK_BASE_URL}/leagues/{slug}"


async def navigate_to_game(page, sport: str, team: str) -> bool:
    """Navigate to the league page and click through to ``team``'s game."""
    url = game_page_url(sport)
    if not url:
        return False
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        # DK uses various selectors for game cards
        game_link = await page.query_selector(f"a:has-text('{team}')")
        if game_link:
            await game_link.click()
            await page.wait_for_timeout(2000)
            return True

        logger.warning(f"Could not find game for {team} on DK page")
        return False
    except Exception as e:
        logger.error(f"Navigation failed: {e}")
        return False
