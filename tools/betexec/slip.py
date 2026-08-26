"""Bet-slip interaction helpers for the DraftKings executor (slice 2 split).

Extracted from ``tools/bet_executor.py``. Pure page orchestration: given a
page and a selection, find the outcome button, fill the stake, confirm, and
screenshot before/after. No DB writes, no Telegram, no arming.

Timestamped screenshot naming is factored out so tests can pin it.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from tools.betexec.config import SCREENSHOT_DIR

logger = logging.getLogger("callisto.executor")

SELECTION_SELECTORS = (
    "button:has-text('{sel}'), "
    "[aria-label*='{sel}'], "
    ".outcome-cell:has-text('{sel}')"
)
STAKE_INPUT_SELECTORS = (
    "input[data-testid='bet-slip-stake'], "
    "input[aria-label*='Wager'], "
    "input[aria-label*='stake'], "
    ".betslip-input input, "
    "input[placeholder*='Wager']"
)
CONFIRM_BUTTON_SELECTORS = (
    "button:has-text('Place Bet'), "
    "button:has-text('Submit'), "
    "button:has-text('Place Wager'), "
    "[data-testid='place-bet-button']"
)
SUCCESS_SELECTORS = (
    ":has-text('Bet Placed'), "
    ":has-text('Wager Accepted'), "
    ":has-text('Bet Confirmed'), "
    ".bet-receipt"
)
ERROR_SELECTORS = (
    ".error-message, .betslip-error, :has-text('odds have changed')"
)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def pre_confirm_path(screenshot_dir: Path = SCREENSHOT_DIR) -> Path:
    return Path(screenshot_dir) / f"pre_confirm_{_ts()}.png"


def confirmed_path(screenshot_dir: Path = SCREENSHOT_DIR) -> Path:
    return Path(screenshot_dir) / f"confirmed_{_ts()}.png"


def error_path(screenshot_dir: Path = SCREENSHOT_DIR) -> Path:
    return Path(screenshot_dir) / f"error_{_ts()}.png"


def build_result() -> dict:
    """Fresh placement-result dict (same shape as the facade's)."""
    return {
        "success": False,
        "screenshot": None,
        "confirmation": None,
        "error": None,
    }


def build_selection_text(market: str, team: str, side: str, point=None) -> str:
    """Selection text for a market type — mirrors the facade's mapping."""
    if market == "h2h":
        return team  # Moneyline — just the team name
    if market == "spreads":
        if point:
            sign = "+" if point > 0 else ""
            return f"{sign}{point}"
        return team
    # totals and anything else use the side ("Over"/"Under"/…)
    return side


async def place_bet_on_slip(page, selection_text: str, stake: float) -> dict:
    """
    Find a betting selection, add to slip, enter stake, and confirm.

    Returns dict with success status, screenshot path, and confirmation details.
    """
    result = build_result()

    try:
        selector = SELECTION_SELECTORS.format(sel=selection_text)
        selection = await page.query_selector(selector)

        if not selection:
            result["error"] = f"Selection '{selection_text}' not found on page"
            return result

        await selection.click()
        await page.wait_for_timeout(1500)

        stake_input = await page.query_selector(STAKE_INPUT_SELECTORS)
        if not stake_input:
            result["error"] = "Could not find stake input in bet slip"
            return result

        # Clear and enter stake
        await stake_input.click(click_count=3)  # Select all
        await stake_input.fill(f"{stake:.2f}")
        await page.wait_for_timeout(500)

        ts = _ts()
        # Screenshot BEFORE confirming (for the record)
        pre_path = SCREENSHOT_DIR / f"pre_confirm_{ts}.png"
        await page.screenshot(path=str(pre_path))

        confirm_btn = await page.query_selector(CONFIRM_BUTTON_SELECTORS)
        if not confirm_btn:
            result["error"] = "Could not find Place Bet button"
            result["screenshot"] = str(pre_path)
            return result

        await confirm_btn.click()
        await page.wait_for_timeout(3000)

        # Screenshot confirmation
        post_path = SCREENSHOT_DIR / f"confirmed_{ts}.png"
        await page.screenshot(path=str(post_path))

        success_el = await page.query_selector(SUCCESS_SELECTORS)

        if success_el:
            result["success"] = True
            result["screenshot"] = str(post_path)
            result["confirmation"] = f"Bet confirmed at {ts}"
        else:
            error_el = await page.query_selector(ERROR_SELECTORS)
            if error_el:
                error_text = await error_el.inner_text()
                result["error"] = f"DK error: {error_text}"
            else:
                # Assume success if no error detected
                result["success"] = True
                result["confirmation"] = (
                    f"Bet submitted at {ts} (no explicit confirmation detected)"
                )
            result["screenshot"] = str(post_path)

        return result

    except Exception as e:
        result["error"] = str(e)
        # Try to screenshot whatever state we're in
        try:
            err_path = error_path()
            await page.screenshot(path=str(err_path))
            result["screenshot"] = str(err_path)
        except Exception as e:
            logger.warning(f"Error screenshot capture also failed: {e}")
        return result
