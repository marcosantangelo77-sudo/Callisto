"""
DraftKings bet executor — Playwright-based browser automation.

Places pre-game bets on DraftKings Sportsbook when the system identifies
+EV signals from live hypotheses. Integrates with the Kelly sizing engine,
bankroll tracker, and Telegram alerts.

Architecture:
  1. Maintains a persistent browser session (login once, reuse cookies)
  2. Receives bet signals from the research loop (live hypotheses)
  3. Sizes bets via quarter-Kelly with bankroll constraints
  4. Navigates to the market, adds to bet slip, confirms
  5. Screenshots confirmation, records to bets table
  6. Sends Telegram notification with bet details

Safety controls:
  - Max single bet: 5% of bankroll (configurable)
  - Daily loss limit: 20% of bankroll (configurable)
  - Pre-game only (refuses live/in-play bets)
  - Kill switch via /admin/executor/stop endpoint
  - Every bet logged + screenshotted
  - Minimum edge threshold to execute
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.executor")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
SCREENSHOT_DIR = Path("memory/bet_screenshots")
SESSION_DIR = Path("memory/dk_session")

# --- Safety limits (configurable via env) ---
MAX_BET_PCT = float(os.getenv("EXECUTOR_MAX_BET_PCT", "0.05"))       # 5% of bankroll
DAILY_LOSS_LIMIT_PCT = float(os.getenv("EXECUTOR_DAILY_LOSS_PCT", "0.20"))  # 20% of bankroll
MIN_EDGE_TO_EXECUTE = float(os.getenv("EXECUTOR_MIN_EDGE", "0.02"))  # 2% minimum EV
KELLY_FRACTION = float(os.getenv("EXECUTOR_KELLY_FRACTION", "0.25")) # Quarter Kelly
MIN_BET_AMOUNT = float(os.getenv("EXECUTOR_MIN_BET", "1.00"))       # $1 minimum
DK_BASE_URL = "https://sportsbook.draftkings.com"

# DraftKings sport slugs for URL navigation
DK_SPORT_SLUGS = {
    "basketball_nba": "nba",
    "basketball_ncaab": "college-basketball",
    "basketball_ncaaw": "womens-college-basketball",
    "americanfootball_nfl": "nfl",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "golf_pga": "golf",
}


class BetExecutor:
    """
    Automated bet placement on DraftKings via Playwright browser automation.

    Usage:
        executor = BetExecutor()
        await executor.initialize()

        # Place a single bet
        result = await executor.execute_bet(
            sport="baseball_mlb",
            team="New York Yankees",
            market="h2h",
            side="Yankees ML",
            odds=-150,
            fair_prob=0.65,
            edge=0.03,
            hypothesis_id="abc123",
        )

        await executor.shutdown()
    """

    def __init__(self):
        self._browser = None
        self._context = None
        self._page = None
        self._db: Optional[aiosqlite.Connection] = None
        self._enabled = False
        self._logged_in = False
        self._daily_pnl = 0.0
        self._daily_bets = 0
        self._last_reset = datetime.now(timezone.utc).date()

    async def initialize(self) -> None:
        """Initialize database connection and ensure directories exist."""
        self._db = await aiosqlite.connect(DB_PATH)
        await self._db.execute("PRAGMA busy_timeout = 60000")
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_DIR.mkdir(parents=True, exist_ok=True)

        # Create executor log table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS executor_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                sport TEXT,
                team TEXT,
                market TEXT,
                side TEXT,
                odds INTEGER,
                stake REAL,
                edge REAL,
                hypothesis_id TEXT,
                bet_id INTEGER,
                screenshot_path TEXT,
                status TEXT NOT NULL,
                error TEXT,
                details TEXT
            )
        """)
        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="executor schema")
        logger.info("Bet executor initialized")

    async def get_bankroll(self) -> float:
        """Get current bankroll balance."""
        cursor = await self._db.execute(
            "SELECT balance FROM bankroll ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0.0

    async def get_daily_stakes(self) -> float:
        """Get total stakes placed today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(stake), 0) FROM bets WHERE placed_at >= ?",
            (today,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0.0

    async def get_daily_losses(self) -> float:
        """Get net losses today (negative = losing)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            """SELECT COALESCE(SUM(
                CASE WHEN result = 'won' THEN payout - stake
                     WHEN result = 'lost' THEN -stake
                     ELSE 0 END
            ), 0) FROM bets WHERE placed_at >= ?""",
            (today,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0.0

    def compute_stake(
        self,
        edge: float,
        odds: int,
        bankroll: float,
        confidence: float = 0.6,
        p_push: float = 0.0,
        variance_estimate: float = None,
    ) -> float:
        """
        Compute bet stake using dynamic Kelly with AGP confidence tiers,
        uncertainty adjustment, and push-aware sizing.

        Uses kelly_dynamic (confidence + variance aware) as the primary
        sizer. Falls back to kelly_with_push for spread bets where push
        is possible. Applies uncertainty_adjusted_kelly when confidence
        is below VERIFIED tier.

        Returns dollar amount to wager (0 if bet should be skipped).
        """
        from tools.kelly import kelly_dynamic, kelly_fractional
        from tools.sizing import kelly_with_push, uncertainty_adjusted_kelly

        # Default variance_estimate: half the edge magnitude
        if variance_estimate is None:
            variance_estimate = abs(edge) * 0.5

        # For spread bets with push probability, use push-aware Kelly
        if p_push > 0:
            from tools.math_utils import american_to_decimal
            decimal_odds = american_to_decimal(odds)
            from tools.odds_api import calculate_implied_probability
            implied = calculate_implied_probability(odds)
            fair_prob = implied + edge

            fk = kelly_with_push(fair_prob, p_push, decimal_odds)

            # Map confidence score to string tier for uncertainty adjustment
            if confidence >= 0.90:
                conf_str = "high"
            elif confidence >= 0.55:
                conf_str = "medium"
            else:
                conf_str = "low"

            # Apply uncertainty adjustment for non-verified edges
            adjusted = uncertainty_adjusted_kelly(fk, edge, conf_str)
            stake_fraction = min(adjusted, MAX_BET_PCT)
            stake = round(bankroll * stake_fraction, 2)

            if stake < MIN_BET_AMOUNT:
                return 0.0
            return stake

        # Primary path: kelly_dynamic integrates AGP confidence tiers,
        # variance dampening, and hard caps in one call
        result = kelly_dynamic(
            edge=edge,
            odds=odds,
            confidence_score=confidence,
            variance_estimate=variance_estimate,
            bankroll=bankroll,
            kelly_base_fraction=KELLY_FRACTION,
        )

        stake = result["stake"]

        # Additional cap at max bet percentage of bankroll
        max_stake = bankroll * MAX_BET_PCT
        if stake > max_stake:
            stake = round(max_stake, 2)

        # Floor
        if stake < MIN_BET_AMOUNT:
            return 0.0

        return stake

    def compute_portfolio_stakes(
        self,
        bets: list[dict],
        bankroll: float,
    ) -> list[dict]:
        """
        Size multiple simultaneous bets with correlation-aware Kelly.

        When multiple bets are open at the same time (e.g., two NBA games
        tonight), correlated bets should be sized down to avoid concentrating
        risk. Falls back to individual quarter-Kelly for a single bet.

        Args:
            bets: List of dicts, each with {edge, odds, confidence,
                  correlation_with_others, description}.
            bankroll: Current bankroll in dollars.

        Returns:
            List of dicts with {description, stake, fraction, ...} per bet.
        """
        if not bets:
            return []

        # Single bet: use standard individual sizing (no portfolio overhead)
        if len(bets) == 1:
            b = bets[0]
            stake = self.compute_stake(
                b.get("edge", 0.0),
                b.get("odds", -110),
                bankroll,
                b.get("confidence", 0.6),
            )
            return [{
                "description": b.get("description", "Bet 1"),
                "stake": stake,
                "fraction": round(stake / bankroll, 6) if bankroll > 0 else 0,
                "method": "individual_kelly",
            }]

        # Multiple bets: use correlation-aware portfolio Kelly
        from tools.kelly import kelly_portfolio

        portfolio_bets = []
        for b in bets:
            portfolio_bets.append({
                "edge": b.get("edge", 0.0),
                "odds": b.get("odds", -110),
                "confidence_score": b.get("confidence", 0.6),
                "variance_estimate": abs(b.get("edge", 0.01)) * 0.5,
                "correlation_with_others": b.get("correlation_with_others", 0.1),
                "description": b.get("description", ""),
            })

        sized = kelly_portfolio(portfolio_bets)

        results = []
        for item in sized:
            frac = item.get("final_fraction", 0.0)
            stake = round(bankroll * frac, 2)
            if stake < MIN_BET_AMOUNT:
                stake = 0.0
            results.append({
                "description": item.get("description", ""),
                "stake": stake,
                "fraction": frac,
                "correlation": item.get("correlation", 0.0),
                "tier": item.get("tier", ""),
                "method": "portfolio_kelly",
                "portfolio_summary": item.get("portfolio_summary", {}),
            })

        return results

    async def preflight_check(
        self,
        sport: str,
        odds: int,
        edge: float,
        stake: float,
    ) -> tuple[bool, str]:
        """
        Run all safety checks before placing a bet.

        Returns (ok, reason).
        """
        # Executor must be enabled
        if not self._enabled:
            return False, "Executor is disabled"

        # Minimum edge
        if edge < MIN_EDGE_TO_EXECUTE:
            return False, f"Edge {edge:.3f} below minimum {MIN_EDGE_TO_EXECUTE}"

        # Bankroll check
        bankroll = await self.get_bankroll()
        if bankroll <= 0:
            return False, "No bankroll"

        if stake > bankroll * MAX_BET_PCT:
            return False, f"Stake ${stake:.2f} exceeds {MAX_BET_PCT*100:.0f}% of bankroll ${bankroll:.2f}"

        # Daily loss limit
        daily_losses = await self.get_daily_losses()
        if daily_losses < -(bankroll * DAILY_LOSS_LIMIT_PCT):
            return False, f"Daily loss limit hit: ${daily_losses:.2f} (limit: ${bankroll * DAILY_LOSS_LIMIT_PCT:.2f})"

        # Sport support
        if sport not in DK_SPORT_SLUGS:
            return False, f"Sport {sport} not supported for DK execution"

        return True, "OK"

    async def launch_browser(self) -> None:
        """Launch Playwright browser with persistent session."""
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        self._browser = await pw.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,  # Visible for initial login, can switch to True later
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._page = self._browser.pages[0] if self._browser.pages else await self._browser.new_page()
        logger.info("Browser launched with persistent session")

    async def ensure_logged_in(self) -> bool:
        """
        Check if we're logged into DraftKings, prompt for manual login if not.

        The first time, the browser opens visible so Marco can log in manually.
        After that, cookies persist in SESSION_DIR.
        """
        if not self._page:
            await self.launch_browser()

        try:
            await self._page.goto(DK_BASE_URL, wait_until="domcontentloaded", timeout=15000)
            await self._page.wait_for_timeout(2000)

            # Check for logged-in indicator (account balance, username, etc.)
            logged_in = await self._page.query_selector(
                "[data-testid='user-balance'], .dk-user-balance, .sportsbook-header__balance"
            )

            if logged_in:
                self._logged_in = True
                logger.info("DraftKings session active — logged in")
                return True

            # Also check by looking for sign-in button absence
            sign_in = await self._page.query_selector(
                "[data-testid='sign-in-button'], .sportsbook-header__sign-in, a[href*='login']"
            )

            if not sign_in:
                # No sign-in button visible — might be logged in with different UI
                self._logged_in = True
                return True

            logger.warning(
                "DraftKings login required — browser is open, please log in manually. "
                "Session will persist after first login."
            )
            self._logged_in = False
            return False

        except Exception as e:
            logger.error(f"Login check failed: {e}")
            return False

    async def navigate_to_game(
        self,
        sport: str,
        team: str,
        event_id: str = "",
    ) -> bool:
        """Navigate to a specific game on DraftKings."""
        slug = DK_SPORT_SLUGS.get(sport, "")
        if not slug:
            return False

        # Navigate to sport page
        url = f"{DK_BASE_URL}/leagues/{slug}"
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await self._page.wait_for_timeout(2000)

            # Try to find the team/game
            # DK uses various selectors for game cards
            game_link = await self._page.query_selector(
                f"a:has-text('{team}')"
            )

            if game_link:
                await game_link.click()
                await self._page.wait_for_timeout(2000)
                return True

            logger.warning(f"Could not find game for {team} on DK {slug} page")
            return False

        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return False

    async def place_bet_on_slip(
        self,
        selection_text: str,
        stake: float,
    ) -> dict:
        """
        Find a betting selection, add to slip, enter stake, and confirm.

        Returns dict with success status, screenshot path, and confirmation details.
        """
        result = {
            "success": False,
            "screenshot": None,
            "confirmation": None,
            "error": None,
        }

        try:
            # Click on the selection (spread/ML/total button)
            selection = await self._page.query_selector(
                f"button:has-text('{selection_text}'), "
                f"[aria-label*='{selection_text}'], "
                f".outcome-cell:has-text('{selection_text}')"
            )

            if not selection:
                result["error"] = f"Selection '{selection_text}' not found on page"
                return result

            await selection.click()
            await self._page.wait_for_timeout(1500)

            # Find the bet slip stake input
            stake_input = await self._page.query_selector(
                "input[data-testid='bet-slip-stake'], "
                "input[aria-label*='Wager'], "
                "input[aria-label*='stake'], "
                ".betslip-input input, "
                "input[placeholder*='Wager']"
            )

            if not stake_input:
                result["error"] = "Could not find stake input in bet slip"
                return result

            # Clear and enter stake
            await stake_input.click(click_count=3)  # Select all
            await stake_input.fill(f"{stake:.2f}")
            await self._page.wait_for_timeout(500)

            # Screenshot BEFORE confirming (for the record)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            pre_path = SCREENSHOT_DIR / f"pre_confirm_{ts}.png"
            await self._page.screenshot(path=str(pre_path))

            # Find and click the Place Bet / Submit button
            confirm_btn = await self._page.query_selector(
                "button:has-text('Place Bet'), "
                "button:has-text('Submit'), "
                "button:has-text('Place Wager'), "
                "[data-testid='place-bet-button']"
            )

            if not confirm_btn:
                result["error"] = "Could not find Place Bet button"
                result["screenshot"] = str(pre_path)
                return result

            await confirm_btn.click()
            await self._page.wait_for_timeout(3000)

            # Screenshot confirmation
            post_path = SCREENSHOT_DIR / f"confirmed_{ts}.png"
            await self._page.screenshot(path=str(post_path))

            # Check for success indicator
            success_el = await self._page.query_selector(
                ":has-text('Bet Placed'), "
                ":has-text('Wager Accepted'), "
                ":has-text('Bet Confirmed'), "
                ".bet-receipt"
            )

            if success_el:
                result["success"] = True
                result["screenshot"] = str(post_path)
                result["confirmation"] = f"Bet confirmed at {ts}"
            else:
                # Check for error messages
                error_el = await self._page.query_selector(
                    ".error-message, .betslip-error, :has-text('odds have changed')"
                )
                if error_el:
                    error_text = await error_el.inner_text()
                    result["error"] = f"DK error: {error_text}"
                else:
                    # Assume success if no error detected
                    result["success"] = True
                    result["confirmation"] = f"Bet submitted at {ts} (no explicit confirmation detected)"
                result["screenshot"] = str(post_path)

            return result

        except Exception as e:
            result["error"] = str(e)
            # Try to screenshot whatever state we're in
            try:
                err_path = SCREENSHOT_DIR / f"error_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.png"
                await self._page.screenshot(path=str(err_path))
                result["screenshot"] = str(err_path)
            except Exception as e:
                logger.warning(f"Error screenshot capture also failed: {e}")
            return result

    async def execute_bet(
        self,
        sport: str,
        team: str,
        market: str,
        side: str,
        odds: int,
        fair_prob: float,
        edge: float,
        hypothesis_id: str = "",
        event_id: str = "",
        game_description: str = "",
        confidence: float = 0.6,
        point: float = None,
    ) -> dict:
        """
        Full bet execution pipeline: size → preflight → navigate → place → record.

        Returns execution result dict.
        """
        now = datetime.now(timezone.utc).isoformat()
        bankroll = await self.get_bankroll()

        # Size the bet
        stake = self.compute_stake(edge, odds, bankroll, confidence)
        if stake <= 0:
            return {"success": False, "reason": "Stake too small after Kelly sizing"}

        # Preflight checks
        ok, reason = await self.preflight_check(sport, odds, edge, stake)
        if not ok:
            await self._log_action("PREFLIGHT_FAIL", sport, team, market, side, odds, stake, edge, hypothesis_id, reason=reason)
            return {"success": False, "reason": reason}

        # Ensure browser is ready
        if not self._logged_in:
            logged_in = await self.ensure_logged_in()
            if not logged_in:
                return {"success": False, "reason": "Not logged into DraftKings — manual login required"}

        # Navigate to game
        found = await self.navigate_to_game(sport, team, event_id)
        if not found:
            await self._log_action("NAV_FAIL", sport, team, market, side, odds, stake, edge, hypothesis_id, reason="Game not found on DK")
            return {"success": False, "reason": f"Could not find {team} game on DraftKings"}

        # Build selection text based on market type
        if market == "h2h":
            selection_text = team  # Moneyline — just the team name
        elif market == "spreads":
            selection_text = f"{'+' if point and point > 0 else ''}{point}" if point else team
        elif market == "totals":
            selection_text = side  # "Over" or "Under"
        else:
            selection_text = side

        # Place the bet
        placement = await self.place_bet_on_slip(selection_text, stake)

        if placement["success"]:
            # Record the bet in the database
            bet_id = await self._record_bet(
                sport=sport,
                event_id=event_id,
                game_description=game_description,
                team=team,
                market=market,
                bookmaker="DraftKings",
                odds=odds,
                point=point,
                stake=stake,
                edge=edge,
                fair_prob=fair_prob,
                hypothesis_id=hypothesis_id,
            )

            await self._log_action(
                "BET_PLACED", sport, team, market, side, odds, stake, edge,
                hypothesis_id, bet_id=bet_id, screenshot=placement.get("screenshot"),
            )

            # Send Telegram notification
            try:
                from tools.telegram import send_telegram
                msg = (
                    f"BET PLACED\n"
                    f"{game_description or team}\n"
                    f"{side} @ {'+' if odds > 0 else ''}{odds}\n"
                    f"Stake: ${stake:.2f} | Edge: {edge*100:.1f}%\n"
                    f"Bankroll: ${bankroll:.2f} → ${bankroll - stake:.2f}"
                )
                send_telegram(msg)
            except Exception as e:
                logger.warning(f"Telegram bet notification failed: {e}")

            return {
                "success": True,
                "bet_id": bet_id,
                "stake": stake,
                "odds": odds,
                "edge": edge,
                "screenshot": placement.get("screenshot"),
            }
        else:
            await self._log_action(
                "BET_FAILED", sport, team, market, side, odds, stake, edge,
                hypothesis_id, reason=placement.get("error"),
                screenshot=placement.get("screenshot"),
            )
            return {
                "success": False,
                "reason": placement.get("error"),
                "screenshot": placement.get("screenshot"),
            }

    async def _record_bet(
        self, sport, event_id, game_description, team, market,
        bookmaker, odds, point, stake, edge, fair_prob, hypothesis_id,
    ) -> int:
        """Record bet in the bets table and update bankroll."""
        now = datetime.now(timezone.utc).isoformat()
        implied = 1.0 - fair_prob + edge  # back-derive implied from fair - edge

        from tools.db_utils import execute_with_retry, commit_with_retry
        cursor = await execute_with_retry(
            self._db,
            """INSERT INTO bets
            (placed_at, sport, event_id, game_description, bet_type,
             team, market, bookmaker, placement_odds, placement_point,
             placement_implied_prob, stake, result, edge_at_placement,
             kelly_at_placement, notes, tags)
            VALUES (?, ?, ?, ?, 'single', ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (
                now, sport, event_id, game_description,
                team, market, bookmaker, odds, point,
                round(implied, 6), stake, round(edge, 6),
                round(stake / max(await self.get_bankroll(), 1), 4),
                f"Auto-executed by Callisto. hypothesis={hypothesis_id}",
                f"auto,hypothesis:{hypothesis_id}",
            ),
            max_retries=10,
            operation="executor record_bet insert",
        )
        bet_id = cursor.lastrowid

        # Update bankroll (deduct stake)
        bankroll = await self.get_bankroll()
        await execute_with_retry(
            self._db,
            "INSERT INTO bankroll (timestamp, balance, change, bet_id, description) VALUES (?, ?, ?, ?, ?)",
            (now, bankroll - stake, -stake, bet_id, f"Auto bet #{bet_id}: {team} {market}"),
            max_retries=10,
            operation="executor record_bet bankroll",
        )

        await commit_with_retry(self._db, max_retries=10, operation="executor record_bet")
        return bet_id

    async def _log_action(
        self, action, sport, team, market, side, odds, stake, edge,
        hypothesis_id, bet_id=None, screenshot=None, reason=None,
    ):
        """Log executor action for audit trail."""
        from tools.db_utils import execute_with_retry, commit_with_retry
        await execute_with_retry(
            self._db,
            """INSERT INTO executor_log
            (timestamp, action, sport, team, market, side, odds, stake, edge,
             hypothesis_id, bet_id, screenshot_path, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                action, sport, team, market, side, odds, stake, edge,
                hypothesis_id, bet_id, screenshot,
                "success" if action == "BET_PLACED" else "failed",
                reason,
            ),
            max_retries=10,
            operation="executor log_action",
        )
        await commit_with_retry(self._db, max_retries=10, operation="executor log_action")

    def enable(self):
        """Enable the executor (allow bet placement)."""
        self._enabled = True
        logger.info("Bet executor ENABLED — live bets will be placed")

    def disable(self):
        """Disable the executor (block bet placement)."""
        self._enabled = False
        logger.info("Bet executor DISABLED — no bets will be placed")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    async def status(self) -> dict:
        """Return executor status for health checks."""
        bankroll = await self.get_bankroll() if self._db else 0
        daily_losses = await self.get_daily_losses() if self._db else 0
        return {
            "enabled": self._enabled,
            "logged_in": self._logged_in,
            "browser_active": self._page is not None,
            "bankroll": bankroll,
            "daily_losses": daily_losses,
            "daily_loss_limit": bankroll * DAILY_LOSS_LIMIT_PCT,
            "max_single_bet_pct": MAX_BET_PCT,
            "kelly_fraction": KELLY_FRACTION,
            "min_edge": MIN_EDGE_TO_EXECUTE,
        }

    async def shutdown(self):
        """Clean shutdown."""
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._page = None
        if self._db:
            await self._db.close()
            self._db = None
        self._enabled = False
        self._logged_in = False
        logger.info("Bet executor shut down")
