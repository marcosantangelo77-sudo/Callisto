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

Split history:
  - Split (2026-08): pure helpers (config constants, DK constants, regime
    lookups, Kelly sizing arithmetic, drawdown evaluation) moved into the
    ``tools.betexec`` package.
  - Slice 2: browser-session flow in ``tools.betexec.browser``, bet-slip
    interaction in ``tools.betexec.slip``, executor_log / bets /
    bankroll-peak DB writes in ``tools.betexec.logging``.
  - Slice 3: portfolio Kelly orchestration (``portfolio``), preflight gates
    (``preflight``), drawdown hypothesis CAS (``kill_switch``), Telegram
    message builders (``notify``).
  - Slice 4 (this): the remaining session/DB orchestration moved out too —
    read-only bankroll/PnL/exposure queries and the status dict assembly in
    ``tools.betexec.db_state``, the full size → cap → preflight → navigate →
    place → record pipeline in ``tools.betexec.execution``, and the
    drawdown kill-switch flow + local-only arm gate + health status
    gathering in ``tools.betexec.lifecycle``.

This module is now a thin facade: it re-exports the authoritative helpers
for backwards compatibility and keeps ``BetExecutor`` as an adapter that
binds its live state (db connection, bankroll lock, enabled flag) to those
modules.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

# --- Facade re-exports from tools.betexec (authoritative home) ---
from tools.betexec.config import (  # noqa: F401
    DB_PATH,
    DAILY_LOSS_LIMIT_PCT,
    DRAWDOWN_PEAK_WINDOW_DAYS,
    KELLY_FRACTION,
    MAX_BET_PCT,
    MAX_DRAWDOWN_PCT,
    MAX_GAME_EXPOSURE_PCT,
    MAX_OPEN_EXPOSURE_PCT,
    MAX_SPORT_EXPOSURE_PCT,
    MIN_BET_AMOUNT,
    MIN_EDGE_TO_EXECUTE,
    REGIME_MAX_MULT as _REGIME_MAX_MULT,
    REGIME_MIN_MULT as _REGIME_MIN_MULT,
    REGIME_SAFETY_ENABLED,
    REGIME_SIZING_ENABLED,
    SCREENSHOT_DIR,
    SESSION_DIR,
    VAR_DAMPENER_HIGH_N as _VAR_DAMPENER_HIGH_N,
    VAR_DAMPENER_LOW_N as _VAR_DAMPENER_LOW_N,
)
from tools.betexec.dk_constants import DK_BASE_URL, DK_SPORT_SLUGS  # noqa: F401
from tools.betexec.regime import clamped_regime_multiplier, regime_safe
from tools.betexec.sizing import (
    apply_exposure_caps,
    build_portfolio_requests,
    compute_stake as _compute_stake_helper,
    signals_n_to_kelly_fraction as _signals_n_to_kelly_fraction_helper,
)
from tools.betexec.drawdown import build_kill_switch_alert, evaluate_drawdown
from tools.betexec import browser as betexec_browser
from tools.betexec import slip as betexec_slip
from tools.betexec import logging as betexec_logging
from tools.betexec import portfolio as betexec_portfolio
from tools.betexec import db_state as betexec_db_state
from tools.betexec import execution as betexec_execution
from tools.betexec import lifecycle as betexec_lifecycle
from tools.betexec.kill_switch import pause_live_hypotheses, attach_pause_result
from tools.betexec.notify import build_bet_placed_message
from tools.betexec.preflight import evaluate_preflight

logger = logging.getLogger("callisto.executor")

# Reload-friendly gate flags: recomputed from env when THIS module is
# reloaded (matches pre-split behaviour where they lived here).
REGIME_SIZING_ENABLED = os.getenv("CALLISTO_REGIME_SIZING", "1") == "1"
REGIME_SAFETY_ENABLED = os.getenv("CALLISTO_REGIME_SAFETY", "1") == "1"


def _clamped_regime_multiplier(sport: str) -> float:
    """Facade wrapper — delegates to tools.betexec.regime.

    Kept as a module-level function so tests can monkeypatch
    ``tools.bet_executor._clamped_regime_multiplier`` and so the gate flag
    consulted is THIS module's (reload/monkeypatch-friendly) attribute.
    """
    return clamped_regime_multiplier(
        sport, gates={"sizing_enabled": globals()["REGIME_SIZING_ENABLED"]}
    )


def _regime_safe(sport: str) -> tuple[bool, str]:
    """Facade wrapper — delegates to tools.betexec.regime."""
    return regime_safe(
        sport, gates={"safety_enabled": globals()["REGIME_SAFETY_ENABLED"]}
    )


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
        # SAFETY: default-disabled. The executor never arms itself; enable()
        # must be called explicitly (and refuses under CALLISTO_LOCAL_ONLY).
        self._enabled = False
        self._logged_in = False
        self._daily_pnl = 0.0
        self._daily_bets = 0
        self._last_reset = datetime.now(timezone.utc).date()
        # SECURITY (audit H-4): serialize the read-bankroll → size-bet → write-bankroll
        # sequence. Without this, two concurrent place_bet calls both read the same
        # balance and both compute stakes against the full bankroll. See record_bet
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

        await betexec_logging.ensure_executor_log_schema(self._db)
        logger.info("Bet executor initialized")

    async def get_bankroll(self) -> float:
        """Get current bankroll balance."""
        return await betexec_db_state.get_bankroll(self._db)

    async def get_daily_stakes(self) -> float:
        """Get total stakes placed today."""
        return await betexec_db_state.get_daily_stakes(self._db)

    async def get_open_exposure(self) -> float:
        """Total stake across all currently-pending bets.

        SECURITY (audit H-1): used as the denominator of the portfolio cap that
        keeps simultaneous bets from compounding past MAX_OPEN_EXPOSURE_PCT of
        bankroll.
        """
        return await betexec_db_state.get_open_exposure(self._db)

    async def get_daily_losses(self) -> float:
        """Get net losses today (negative = losing)."""
        return await betexec_db_state.get_daily_losses(self._db)

    def compute_stake(
        self,
        edge: float,
        odds: int,
        bankroll: float,
        confidence: float = 0.6,
        p_push: float = 0.0,
        variance_estimate: Optional[float] = None,
    ) -> float:
        """Compute a single-bet stake — canonical Kelly only (source contract).

        The pure implementation also lives in tools.betexec.sizing; the body
        is kept inline here because a source-contract test pins that
        ``BetExecutor.compute_stake`` itself imports from tools.kelly /
        tools.sizing.
        """
        # Canonical Kelly module is tools.kelly; tools.sizing only provides
        # push-aware helpers with no canonical equivalent.
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
        """Map observed-signals count to Kelly base fraction — see tools.betexec.sizing."""
        return _signals_n_to_kelly_fraction_helper(signals_n)

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
        return betexec_portfolio.compute_portfolio_stakes(
            bets,
            bankroll,
            correlation_matrix,
            regime_multiplier_fn=_clamped_regime_multiplier,
            kelly_fraction_fn=self._signals_n_to_kelly_fraction,
            stake_fn=self.compute_stake,
        )

    async def preflight_check(
        self,
        sport: str,
        odds: int,
        edge: float,
        stake: float,
    ) -> tuple[bool, str]:
        """
        Run all safety checks before placing a bet.

        Returns (ok, reason). The gate logic lives in
        tools.betexec.preflight.evaluate_preflight; this method only gathers
        the live DB values and delegates.
        """
        # Enablement gate first — refuse before any DB access (no db needed).
        if not self._enabled:
            return False, "Executor is disabled"
        bankroll = await self.get_bankroll()
        daily_losses = await self.get_daily_losses()
        return evaluate_preflight(
            enabled=self._enabled,
            edge=edge,
            bankroll=bankroll,
            stake=stake,
            daily_losses=daily_losses,
            sport=sport,
        )

    async def launch_browser(self) -> None:
        """Launch Playwright browser with persistent session."""
        self._context, self._page = await betexec_browser.launch_persistent_session(
            SESSION_DIR
        )
        self._browser = self._context

    async def ensure_logged_in(self) -> bool:
        """
        Check if we're logged into DraftKings, prompt for manual login if not.

        The first time, the browser opens visible so Marco can log in manually.
        After that, cookies persist in SESSION_DIR.
        """
        if not self._page:
            await self.launch_browser()

        ok = await betexec_browser.check_logged_in(self._page)
        self._logged_in = ok
        return ok

    async def navigate_to_game(
        self,
        sport: str,
        team: str,
        event_id: str = "",
    ) -> bool:
        """Navigate to a specific game on DraftKings."""
        return await betexec_browser.navigate_to_game(self._page, sport, team)

    async def place_bet_on_slip(
        self,
        selection_text: str,
        stake: float,
    ) -> dict:
        """
        Find a betting selection, add to slip, enter stake, and confirm.

        Returns dict with success status, screenshot path, and confirmation details.
        """
        return await betexec_slip.place_bet_on_slip(self._page, selection_text, stake)

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

        Returns execution result dict. The pipeline body lives in
        ``tools.betexec.execution.run_execute_bet``; this adapter binds the
        executor's live state and browser hooks to it.

        SECURITY (audit H-1, H-4): the read-bankroll → size → exposure-check → write
        sequence remains serialized by ``self._bankroll_lock`` inside the pipeline.
        """
        async def _ensure_logged_in():
            # Preserve legacy short-circuit: an already-known-good session
            # never touches the browser (matches pre-split facade check).
            if self._logged_in:
                return True
            return await self.ensure_logged_in()

        return await betexec_execution.run_execute_bet(
            db=self._db,
            bankroll_lock=self._bankroll_lock,
            enabled=self._enabled,
            sport=sport,
            team=team,
            market=market,
            side=side,
            odds=odds,
            fair_prob=fair_prob,
            edge=edge,
            hypothesis_id=hypothesis_id,
            event_id=event_id,
            game_description=game_description,
            confidence=confidence,
            point=point,
            stake_override=stake_override,
            compute_stake_fn=self.compute_stake,
            preflight_fn=self.preflight_check,
            ensure_logged_in_fn=_ensure_logged_in,
            navigate_fn=self.navigate_to_game,
            place_fn=self.place_bet_on_slip,
            record_bet_fn=self._record_bet,
            log_action_fn=self._log_action,
            notify_fn=self._notify,
            build_message_fn=build_bet_placed_message,
        )

    @staticmethod
    def _notify(msg: str) -> None:
        """Best-effort Telegram send — imported lazily so missing webhook config
        never blocks bet recording."""
        from tools.telegram import send_telegram
        send_telegram(msg)

    async def _record_bet(
        self, sport, event_id, game_description, team, market,
        bookmaker, odds, point, stake, edge, fair_prob, hypothesis_id,
    ) -> int:
        """Record bet in the bets table and update bankroll."""
        return await betexec_logging.record_bet(
            self._db,
            self.get_bankroll,
            self._bankroll_lock,
            sport=sport,
            event_id=event_id,
            game_description=game_description,
            team=team,
            market=market,
            bookmaker=bookmaker,
            odds=odds,
            point=point,
            stake=stake,
            edge=edge,
            fair_prob=fair_prob,
            hypothesis_id=hypothesis_id,
        )

    # ------------------------------------------------------------------
    # Drawdown kill-switch (feat/portfolio-kelly-live-loop, audit 2026-04-22)
    # ------------------------------------------------------------------
    async def _record_bankroll_peak(self, bankroll: float) -> None:
        """Record an observation of bankroll into the peak table.

        Called opportunistically by ``check_drawdown_and_kill``. The table is
        append-only so a 30d peak is a simple MAX over the window.
        """
        await betexec_logging.record_bankroll_peak(self._db, bankroll)

    async def _rolling_peak(self, window_days: int = None) -> float:
        """Return MAX(balance) over the rolling peak window."""
        return await betexec_logging.rolling_peak(self._db, window_days)

    async def check_drawdown_and_kill(self) -> dict:
        """Evaluate rolling drawdown; if past MAX_DRAWDOWN_PCT, kill-switch.

        Flow lives in ``tools.betexec.lifecycle.run_check_drawdown_and_kill``;
        this adapter supplies the db handle and the disarm callback.
        """
        return await betexec_lifecycle.run_check_drawdown_and_kill(
            self._db,
            disable_fn=self.disable,
        )

    async def _log_action(
        self, action, sport, team, market, side, odds, stake, edge,
        hypothesis_id, bet_id=None, screenshot=None, reason=None,
    ):
        """Log executor action for audit trail."""
        await betexec_logging.log_action(
            self._db, action, sport, team, market, side, odds, stake, edge,
            hypothesis_id, bet_id=bet_id, screenshot=screenshot, reason=reason,
        )

    def enable(self) -> bool:
        """Enable the executor (allow bet placement).

        Returns True when the executor was armed, False when refused.
        Refuses to arm when CALLISTO_LOCAL_ONLY is truthy — that env var is
        the appliance-wide nuclear switch and must block live betting too.
        The refusal is evaluated BEFORE any state flip (gate lives in
        ``tools.betexec.lifecycle.arm_gate_refusal``).
        """
        refusal = betexec_lifecycle.arm_gate_refusal()
        if refusal:
            logger.warning(refusal)
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
        """Return executor status for health checks (assembly lives in
        ``tools.betexec.lifecycle.run_status``)."""
        return await betexec_lifecycle.run_status(
            self._db,
            enabled=self._enabled,
            logged_in=self._logged_in,
            browser_active=self._page is not None,
        )

    async def shutdown(self):
        """Clean shutdown."""
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._context = None
            self._page = None
        if self._db:
            await self._db.close()
            self._db = None
        self._enabled = False
        self._logged_in = False
        logger.info("Bet executor shut down")
