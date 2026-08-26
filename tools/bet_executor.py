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
MAX_BET_PCT = float(os.getenv("EXECUTOR_MAX_BET_PCT", "0.05"))       # 5% of bankroll per bet
# SECURITY (audit H-1): hard ceiling on the SUM of all currently-pending stakes.
# Per-bet caps don't prevent ruin when N concurrent bets clear simultaneously.
# 25% bankroll exposed at any moment is the documented ceiling; raise via env.
MAX_OPEN_EXPOSURE_PCT = float(os.getenv("EXECUTOR_MAX_OPEN_EXPOSURE_PCT", "0.25"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("EXECUTOR_DAILY_LOSS_PCT", "0.20"))  # 20% of bankroll
MIN_EDGE_TO_EXECUTE = float(os.getenv("EXECUTOR_MIN_EDGE", "0.02"))  # 2% minimum EV
KELLY_FRACTION = float(os.getenv("EXECUTOR_KELLY_FRACTION", "0.25")) # Quarter Kelly
MIN_BET_AMOUNT = float(os.getenv("EXECUTOR_MIN_BET", "1.00"))       # $1 minimum

# --- Portfolio-level caps (feat/portfolio-kelly-live-loop, audit 2026-04-22) ---
# Prevents N LIVE hyps from all loading up on one MLB game. Per-game cap
# scales ALL stakes on the same event_id if their sum would exceed bankroll * cap.
MAX_GAME_EXPOSURE_PCT = float(os.getenv("CALLISTO_MAX_GAME_EXPOSURE_PCT", "0.08"))
# Per-sport cap: prevent all-MLB days from pushing too much on baseball.
MAX_SPORT_EXPOSURE_PCT = float(os.getenv("CALLISTO_MAX_SPORT_EXPOSURE_PCT", "0.15"))

# --- Drawdown kill switch (feat/portfolio-kelly-live-loop, audit 2026-04-22) ---
# If bankroll drops more than MAX_DRAWDOWN_PCT below the 30-day peak, flip
# _enabled=False on the executor AND set all LIVE hyps to 'drawdown_paused'.
# Recovery is MANUAL — auto-resume is intentionally not implemented.
MAX_DRAWDOWN_PCT = float(os.getenv("CALLISTO_MAX_DRAWDOWN_PCT", "0.15"))
DRAWDOWN_PEAK_WINDOW_DAYS = int(os.getenv("CALLISTO_DRAWDOWN_WINDOW_DAYS", "30"))

# --- Variance-dampener boundaries tied to paper-trade sample size ---
# < 25 signals: fresh evidence, force half-Kelly (0.125 base fraction)
# >= 100 signals: full quarter-Kelly allowed (0.25 base fraction)
# Smooth linear interp in between.
_VAR_DAMPENER_LOW_N = int(os.getenv("CALLISTO_VAR_DAMPENER_LOW_N", "25"))
_VAR_DAMPENER_HIGH_N = int(os.getenv("CALLISTO_VAR_DAMPENER_HIGH_N", "100"))

# --- Regime-aware sizing (feat/regime-aware-sizing, 2026-04-22) ---
# Scale each bet's stake by its sport's current regime multiplier so
# exposure reflects season phase + volatility. Hard floor/ceiling enforced
# in-module even if market_regime returns an out-of-range value.
REGIME_SIZING_ENABLED = os.getenv("CALLISTO_REGIME_SIZING", "1") == "1"
REGIME_SAFETY_ENABLED = os.getenv("CALLISTO_REGIME_SAFETY", "1") == "1"
_REGIME_MIN_MULT = 0.1   # never zero-size a live bet; use safety gate for that
_REGIME_MAX_MULT = 1.5   # cap upside even in the best regime


def _clamped_regime_multiplier(sport: str) -> float:
    """Fetch current_regime_multiplier(sport) and clamp to [_REGIME_MIN_MULT, _REGIME_MAX_MULT].

    Any exception (DB missing, import error) degrades to 1.0 so sizing never
    fails closed due to the regime module. The whole feature is gated by
    CALLISTO_REGIME_SIZING so callers can disable wholesale.
    """
    if not REGIME_SIZING_ENABLED:
        return 1.0
    try:
        from tools.market_regime import current_regime_multiplier
        m = float(current_regime_multiplier(sport))
    except Exception as e:
        logger.debug(f"regime multiplier lookup failed for {sport}: {e}; using 1.0")
        return 1.0
    return max(_REGIME_MIN_MULT, min(_REGIME_MAX_MULT, m))


def _regime_safe(sport: str) -> tuple[bool, str]:
    """Return (safe, phase) for ``sport``. Safe=True when gate disabled or OK.

    Second value is the season_phase string so callers can include it in log
    lines (``regime_unsafe_phase=preseason`` etc). Any error degrades to safe.
    """
    if not REGIME_SAFETY_ENABLED:
        return True, ""
    try:
        from tools.market_regime import regime_safe_for_trading, detect_regime
        safe = bool(regime_safe_for_trading(sport))
        phase = ""
        if not safe:
            try:
                phase = detect_regime(sport).season_phase or ""
            except Exception:
                phase = ""
        return safe, phase
    except Exception as e:
        logger.debug(f"regime safety lookup failed for {sport}: {e}; treating as safe")
        return True, ""

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
        # SECURITY (audit H-4): serialize the read-bankroll → size-bet → write-bankroll
        # sequence. Without this, two concurrent place_bet calls both read the same
        # balance and both compute stakes against the full bankroll. See _record_bet
        # and place_bet for the holders.
        self._bankroll_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize database connection and ensure directories exist."""
        self._db = await aiosqlite.connect(DB_PATH)
        # Tag for WriteCoordinator routing (single-writer pattern).
        from tools.db_writer import tag_connection as _tag
        _tag(self._db, DB_PATH)
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

    async def get_open_exposure(self) -> float:
        """Total stake across all currently-pending bets.

        SECURITY (audit H-1): used as the denominator of the portfolio cap that
        keeps simultaneous bets from compounding past MAX_OPEN_EXPOSURE_PCT of
        bankroll.
        """
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(stake), 0) FROM bets WHERE result = 'pending'"
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

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

    @staticmethod
    def _signals_n_to_kelly_fraction(signals_n: int) -> float:
        """Map observed-signals count to Kelly base fraction.

        feat/portfolio-kelly-live-loop (audit 2026-04-22): half-Kelly for
        hypotheses with fewer than _VAR_DAMPENER_LOW_N signals, full quarter-
        Kelly once they cross _VAR_DAMPENER_HIGH_N. Linear interp between.
        """
        if signals_n <= _VAR_DAMPENER_LOW_N:
            return 0.125  # half-Kelly relative to quarter-Kelly floor
        if signals_n >= _VAR_DAMPENER_HIGH_N:
            return 0.25  # full quarter-Kelly
        # Linear interpolation between 0.125 and 0.25
        span = max(1, _VAR_DAMPENER_HIGH_N - _VAR_DAMPENER_LOW_N)
        t = (signals_n - _VAR_DAMPENER_LOW_N) / span
        return 0.125 + t * (0.25 - 0.125)

    def compute_portfolio_stakes(
        self,
        bets: list[dict],
        bankroll: float,
        correlation_matrix: Optional[dict] = None,
    ) -> list[dict]:
        """
        Size multiple simultaneous bets with correlation-aware Kelly.

        feat/portfolio-kelly-live-loop (audit 2026-04-22): now the primary
        sizing path for the live-execution loop. Enforces per-game and
        per-sport exposure caps on top of correlation-aware Kelly.

        Args:
            bets: List of dicts, each with {edge, odds, confidence,
                  correlation_with_others, description, event_id, sport,
                  market_type, hypothesis_id, signals_n}.
            bankroll: Current bankroll in dollars.
            correlation_matrix: Optional {(hyp_a, hyp_b): float}. If present,
                this overrides per-bet ``correlation_with_others`` by looking
                up the mean pairwise correlation of each bet with every other
                bet in the batch.

        Returns:
            List of dicts with {description, stake, fraction, event_id, sport,
            method, portfolio_summary} per bet.
        """
        if not bets:
            return []

        # --- Regime multipliers per sport in the batch (cached for this call) ---
        # Compute once per distinct sport so a 20-bet batch hits the regime
        # module at most len(set(sports)) times rather than 20.
        sports_in_batch = {b.get("sport", "") for b in bets if b.get("sport")}
        regime_mults: dict[str, float] = {
            sp: _clamped_regime_multiplier(sp) for sp in sports_in_batch
        }
        if regime_mults:
            logger.info(
                "regime_sizing: applying multipliers %s (enabled=%s)",
                {k: round(v, 3) for k, v in regime_mults.items()},
                REGIME_SIZING_ENABLED,
            )

        # Single bet: use standard individual sizing (no portfolio overhead).
        if len(bets) == 1:
            b = bets[0]
            signals_n = int(b.get("signals_n", 0) or 0)
            kelly_frac = self._signals_n_to_kelly_fraction(signals_n)
            stake = self.compute_stake(
                b.get("edge", 0.0),
                b.get("odds", -110),
                bankroll,
                b.get("confidence", 0.6),
            )
            # Scale by signals_n-aware base fraction (cap-at-quarter Kelly).
            stake = round(stake * (kelly_frac / KELLY_FRACTION), 2) if KELLY_FRACTION > 0 else stake
            # Regime multiplier (feat/regime-aware-sizing).
            sport = b.get("sport", "")
            reg_mult = regime_mults.get(sport, 1.0)
            pre_regime_stake = stake
            stake = round(stake * reg_mult, 2)
            if reg_mult != 1.0 and pre_regime_stake >= MIN_BET_AMOUNT:
                logger.info(
                    "regime_sizing: %s stake ${%.2f} → ${%.2f} (mult=%.3f sport=%s)",
                    b.get("hypothesis_id", "?"), pre_regime_stake, stake,
                    reg_mult, sport,
                )
            return [{
                "description": b.get("description", "Bet 1"),
                "stake": stake if stake >= MIN_BET_AMOUNT else 0.0,
                "fraction": round(stake / bankroll, 6) if bankroll > 0 else 0,
                "event_id": b.get("event_id", ""),
                "sport": sport,
                "hypothesis_id": b.get("hypothesis_id", ""),
                "method": "individual_kelly_n_adjusted",
                "kelly_base_fraction": kelly_frac,
                "signals_n": signals_n,
                "regime_multiplier": reg_mult,
                "stake_before_regime": pre_regime_stake,
            }]

        # Multiple bets: use correlation-aware portfolio Kelly.
        from tools.kelly import kelly_portfolio

        # If a correlation matrix was passed, override per-bet
        # ``correlation_with_others`` with the average pairwise correlation
        # of each bet with every other bet in the batch. This is what the
        # audit wants: correlations derived from historical co-firing.
        corr_overrides: dict[int, float] = {}
        if correlation_matrix:
            n = len(bets)
            for i, bi in enumerate(bets):
                hi = bi.get("hypothesis_id", "")
                if not hi:
                    continue
                pair_corrs = []
                for j, bj in enumerate(bets):
                    if i == j:
                        continue
                    hj = bj.get("hypothesis_id", "")
                    if not hj:
                        continue
                    key = (hi, hj) if (hi, hj) in correlation_matrix else (hj, hi)
                    if key in correlation_matrix:
                        pair_corrs.append(correlation_matrix[key])
                if pair_corrs:
                    corr_overrides[i] = sum(pair_corrs) / len(pair_corrs)

        portfolio_bets = []
        for i, b in enumerate(bets):
            rho = corr_overrides.get(i, b.get("correlation_with_others", 0.1))
            portfolio_bets.append({
                "edge": b.get("edge", 0.0),
                "odds": b.get("odds", -110),
                "confidence_score": b.get("confidence", 0.6),
                "variance_estimate": abs(b.get("edge", 0.01)) * 0.5,
                "correlation_with_others": rho,
                "description": b.get("description", ""),
            })

        sized = kelly_portfolio(portfolio_bets)

        # First pass: compute raw stakes, apply signals_n dampener + regime
        # multiplier, floor.
        results: list[dict] = []
        for i, item in enumerate(sized):
            b = bets[i]
            frac = float(item.get("final_fraction", 0.0))
            signals_n = int(b.get("signals_n", 0) or 0)
            # Blend in the signals_n dampener: scale the portfolio-Kelly
            # fraction by (kelly_frac / KELLY_FRACTION) — so a fresh hyp
            # (signals_n<25) gets half its correlation-aware allocation.
            kelly_frac = self._signals_n_to_kelly_fraction(signals_n)
            scale = (kelly_frac / KELLY_FRACTION) if KELLY_FRACTION > 0 else 1.0
            frac = frac * scale
            stake_before_regime = round(bankroll * frac, 2)
            # Regime multiplier (feat/regime-aware-sizing).
            sport = b.get("sport", "")
            reg_mult = regime_mults.get(sport, 1.0)
            frac = frac * reg_mult
            stake = round(bankroll * frac, 2)
            if reg_mult != 1.0 and stake_before_regime > 0:
                logger.info(
                    "regime_sizing: %s stake ${%.2f} → ${%.2f} "
                    "(mult=%.3f sport=%s)",
                    b.get("hypothesis_id", "?"), stake_before_regime, stake,
                    reg_mult, sport,
                )
            results.append({
                "description": item.get("description", ""),
                "stake": stake,
                "fraction": frac,
                "correlation": item.get("correlation", 0.0),
                "tier": item.get("tier", ""),
                "event_id": b.get("event_id", ""),
                "sport": sport,
                "hypothesis_id": b.get("hypothesis_id", ""),
                "market_type": b.get("market_type", ""),
                "method": "portfolio_kelly_n_adjusted",
                "kelly_base_fraction": kelly_frac,
                "signals_n": signals_n,
                "regime_multiplier": reg_mult,
                "stake_before_regime": stake_before_regime,
                "portfolio_summary": item.get("portfolio_summary", {}),
            })

        # Second pass: per-game exposure cap.
        game_cap = bankroll * MAX_GAME_EXPOSURE_PCT
        by_game: dict[str, list[int]] = {}
        for idx, r in enumerate(results):
            eid = r.get("event_id") or ""
            if not eid:
                continue
            by_game.setdefault(eid, []).append(idx)
        for eid, idxs in by_game.items():
            total = sum(results[i]["stake"] for i in idxs)
            if total > game_cap and total > 0:
                scale = game_cap / total
                for i in idxs:
                    results[i]["stake"] = round(results[i]["stake"] * scale, 2)
                    results[i]["fraction"] = results[i]["fraction"] * scale
                    results[i]["game_cap_scale"] = round(scale, 4)

        # Third pass: per-sport exposure cap.
        sport_cap = bankroll * MAX_SPORT_EXPOSURE_PCT
        by_sport: dict[str, list[int]] = {}
        for idx, r in enumerate(results):
            sp = r.get("sport") or ""
            if not sp:
                continue
            by_sport.setdefault(sp, []).append(idx)
        for sp, idxs in by_sport.items():
            total = sum(results[i]["stake"] for i in idxs)
            if total > sport_cap and total > 0:
                scale = sport_cap / total
                for i in idxs:
                    results[i]["stake"] = round(results[i]["stake"] * scale, 2)
                    results[i]["fraction"] = results[i]["fraction"] * scale
                    results[i]["sport_cap_scale"] = round(scale, 4)

        # Final pass: floor below MIN_BET_AMOUNT.
        for r in results:
            if r["stake"] < MIN_BET_AMOUNT:
                r["stake"] = 0.0

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
        stake_override: Optional[float] = None,
    ) -> dict:
        """
        Full bet execution pipeline: size → preflight → navigate → place → record.

        Returns execution result dict.

        feat/portfolio-kelly-live-loop (audit 2026-04-22): callers can pass
        ``stake_override`` to use a pre-computed portfolio stake and skip the
        per-bet Kelly sizing. The portfolio caller (``size_portfolio`` in the
        live-exec loop) already applied correlation, game/sport caps, and the
        signals_n dampener; recomputing here would undo that work.

        SECURITY (audit H-1, H-4): the read-bankroll → size → exposure-check → write
        sequence is serialized by ``self._bankroll_lock`` so two concurrent placements
        cannot both decide they have full bankroll available, and the portfolio cap
        (``MAX_OPEN_EXPOSURE_PCT``) is evaluated against the *committed* sum of pending
        stakes rather than a stale snapshot.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with self._bankroll_lock:
            bankroll = await self.get_bankroll()

            # Size the bet — respect pre-computed portfolio stake if provided.
            if stake_override is not None and stake_override > 0:
                stake = round(float(stake_override), 2)
            else:
                stake = self.compute_stake(edge, odds, bankroll, confidence)
            if stake <= 0:
                return {"success": False, "reason": "Stake too small after Kelly sizing"}

            # Portfolio-level cap: refuse if this stake would push total open
            # exposure past MAX_OPEN_EXPOSURE_PCT of bankroll.
            open_exposure = await self.get_open_exposure()
            exposure_cap = bankroll * MAX_OPEN_EXPOSURE_PCT
            if open_exposure + stake > exposure_cap:
                room = max(0.0, exposure_cap - open_exposure)
                if room < MIN_BET_AMOUNT:
                    await self._log_action(
                        "EXPOSURE_CAP", sport, team, market, side, odds, stake, edge,
                        hypothesis_id,
                        reason=f"Open exposure ${open_exposure:.2f} + stake ${stake:.2f} > cap ${exposure_cap:.2f}",
                    )
                    return {
                        "success": False,
                        "reason": (
                            f"Portfolio exposure cap hit: ${open_exposure:.2f} pending + "
                            f"${stake:.2f} would exceed {MAX_OPEN_EXPOSURE_PCT:.0%} of bankroll"
                        ),
                    }
                # Shrink stake to the remaining headroom rather than skipping outright.
                stake = round(room, 2)

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
        # SECURITY/CORRECTNESS: implied = fair_prob - edge (audit C-3, 2026-04-18).
        # Prior formula `1.0 - fair_prob + edge` was inverted, poisoning every CLV calc.
        implied = max(0.0, min(1.0, fair_prob - edge))

        from tools.db_utils import execute_with_retry, commit_with_retry

        # Guard against duplicate bets: check if we already have a pending bet
        # on this event+team+market within the last hour
        dup_check = await execute_with_retry(
            self._db,
            "SELECT bet_id FROM bets WHERE event_id = ? AND team = ? AND market = ? "
            "AND result = 'pending' AND placed_at > datetime('now', '-1 hour')",
            (event_id, team, market),
            operation="executor dup_check",
        )
        existing = await dup_check.fetchone()
        if existing:
            logger.warning(
                f"Duplicate bet prevented: event={event_id} team={team} market={market} "
                f"(existing bet_id={existing[0]})"
            )
            return existing[0]

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

        # Update bankroll (deduct stake) under the same lock that gated sizing,
        # so the read→write of bankroll is atomic across concurrent placements
        # (audit H-4). feat/portfolio-kelly-live-loop (audit 2026-04-22):
        # wrap read+insert in BEGIN IMMEDIATE so SQLite's own locking
        # serializes even if two callers somehow race past the asyncio lock
        # (e.g. different event loops, or the fallback path when the
        # WriteCoordinator is not running).
        async with self._bankroll_lock:
            # BEGIN IMMEDIATE acquires a RESERVED lock now, preventing any
            # other writer from reading-then-writing on top of our snapshot.
            try:
                await self._db.execute("BEGIN IMMEDIATE")
            except Exception:
                # If a transaction is already open (WriteCoordinator path),
                # BEGIN will raise — that's fine, the coordinator serializes
                # writes already.
                pass
            try:
                bankroll = await self.get_bankroll()
                await execute_with_retry(
                    self._db,
                    "INSERT INTO bankroll (timestamp, balance, change, bet_id, description) VALUES (?, ?, ?, ?, ?)",
                    (now, bankroll - stake, -stake, bet_id, f"Auto bet #{bet_id}: {team} {market}"),
                    max_retries=10,
                    operation="executor record_bet bankroll",
                )

                await commit_with_retry(self._db, max_retries=10, operation="executor record_bet")
            except Exception:
                try:
                    await self._db.rollback()
                except Exception:
                    pass
                raise
        return bet_id

    # ------------------------------------------------------------------
    # Drawdown kill-switch (feat/portfolio-kelly-live-loop, audit 2026-04-22)
    # ------------------------------------------------------------------
    async def _record_bankroll_peak(self, bankroll: float) -> None:
        """Record an observation of bankroll into the peak table.

        Called opportunistically by ``check_drawdown_and_kill``. The table is
        append-only so a 30d peak is a simple MAX over the window.
        """
        try:
            from tools.db_utils import execute_with_retry, commit_with_retry
            await execute_with_retry(
                self._db,
                "INSERT INTO bankroll_peak (observed_at, balance, note) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), float(bankroll), "auto-observed"),
                operation="bankroll_peak insert",
            )
            await commit_with_retry(self._db, operation="bankroll_peak insert")
        except Exception as e:
            logger.debug(f"bankroll_peak insert skipped: {e}")

    async def _rolling_peak(self, window_days: int = None) -> float:
        """Return MAX(balance) over the rolling peak window."""
        window = window_days or DRAWDOWN_PEAK_WINDOW_DAYS
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()
        try:
            cursor = await self._db.execute(
                "SELECT COALESCE(MAX(balance), 0) FROM bankroll_peak WHERE observed_at >= ?",
                (cutoff,),
            )
            row = await cursor.fetchone()
            peak = float(row[0]) if row and row[0] is not None else 0.0
        except Exception:
            peak = 0.0
        # Fallback to bankroll history if bankroll_peak is empty / missing.
        if peak <= 0:
            try:
                cursor = await self._db.execute(
                    "SELECT COALESCE(MAX(balance), 0) FROM bankroll WHERE timestamp >= ?",
                    (cutoff,),
                )
                row = await cursor.fetchone()
                peak = float(row[0]) if row and row[0] is not None else 0.0
            except Exception:
                peak = 0.0
        return peak

    async def check_drawdown_and_kill(self) -> dict:
        """Evaluate rolling drawdown; if past MAX_DRAWDOWN_PCT, kill-switch.

        Flow:
          1. Read current bankroll and rolling peak.
          2. Record current bankroll into bankroll_peak (so next call has a
             growing history).
          3. If current < peak * (1 - MAX_DRAWDOWN_PCT):
             - self._enabled = False
             - CAS all LIVE hyps to 'drawdown_paused'
             - fire Telegram alert (best-effort; missing webhook is fine)

        Returns a status dict describing the action taken.
        """
        current = await self.get_bankroll()
        peak = await self._rolling_peak()
        await self._record_bankroll_peak(current)

        status: dict = {
            "current_bankroll": current,
            "rolling_peak": peak,
            "drawdown_pct": 0.0,
            "threshold_pct": MAX_DRAWDOWN_PCT,
            "triggered": False,
            "paused_hypotheses": [],
        }

        if peak <= 0 or current >= peak:
            return status

        drawdown_pct = (peak - current) / peak
        status["drawdown_pct"] = round(drawdown_pct, 4)

        if drawdown_pct < MAX_DRAWDOWN_PCT:
            return status

        # Kill switch fires.
        logger.error(
            f"DRAWDOWN KILL SWITCH: current=${current:,.2f} peak=${peak:,.2f} "
            f"drawdown={drawdown_pct:.1%} exceeds threshold {MAX_DRAWDOWN_PCT:.1%}"
        )
        self._enabled = False
        status["triggered"] = True

        # CAS all LIVE hypotheses to drawdown_paused.
        try:
            from tools.db_utils import execute_with_retry, commit_with_retry
            cursor = await self._db.execute(
                "SELECT hypothesis_id FROM hypotheses WHERE status = 'live'"
            )
            live_rows = await cursor.fetchall()
            now_ts = datetime.now(timezone.utc).isoformat()
            paused = []
            for row in live_rows:
                hid = row[0]
                res = await execute_with_retry(
                    self._db,
                    "UPDATE hypotheses SET status = 'drawdown_paused', updated_at = ?, "
                    "promoted_at = ?, promoted_by = ? "
                    "WHERE hypothesis_id = ? AND status = 'live'",
                    (now_ts, now_ts, "drawdown_kill_switch", hid),
                    operation="drawdown pause hypothesis",
                )
                if (res.rowcount or 0) > 0:
                    paused.append(hid)
            await commit_with_retry(self._db, operation="drawdown pause hypotheses")
            status["paused_hypotheses"] = paused
        except Exception as e:
            logger.error(f"Drawdown: failed to pause LIVE hypotheses: {e}")

        # Best-effort Telegram alert.
        try:
            from tools.telegram import send_alert  # noqa: WPS433
            msg = (
                f"<b>DRAWDOWN KILL SWITCH FIRED</b>\n"
                f"\n"
                f"Current bankroll: ${current:,.2f}\n"
                f"30d peak: ${peak:,.2f}\n"
                f"Drawdown: {drawdown_pct:.1%} (threshold {MAX_DRAWDOWN_PCT:.1%})\n"
                f"\n"
                f"Executor disabled. {len(status['paused_hypotheses'])} LIVE "
                f"hyps → drawdown_paused. Manual review required."
            )
            await send_alert(msg, throttle_key="drawdown_kill")
        except Exception as e:
            logger.debug(f"Telegram drawdown alert skipped: {e}")

        return status

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

    def enable(self) -> bool:
        """Enable the executor (allow bet placement).

        Returns True when the executor was armed, False when refused.
        Refuses to arm when CALLISTO_LOCAL_ONLY is truthy — that env var is
        the appliance-wide nuclear switch and must block live betting too.
        """
        if os.getenv("CALLISTO_LOCAL_ONLY", "").lower() in ("1", "true", "yes"):
            logger.warning(
                "Bet executor NOT enabled: CALLISTO_LOCAL_ONLY is set — "
                "local-only mode refuses to arm live betting"
            )
            return False
        self._enabled = True
        logger.info("Bet executor ENABLED — live bets will be placed")
        return True

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
