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

Split (2026-08): pure helpers (config constants, DK constants, regime
lookups, Kelly sizing arithmetic, drawdown evaluation) live in the
``tools.betexec`` package; this module is the facade that re-exports them
and keeps the ``BetExecutor`` orchestration + DB/browser surface.

Slice 2 split (2026-08): the Playwright browser-session flow lives in
``tools.betexec.browser``, the bet-slip interaction in ``tools.betexec.slip``,
and the executor_log / bets / bankroll-peak DB writes in
``tools.betexec.logging`` (imported here as ``betexec_logging``). This module
keeps ``BetExecutor`` as a thin delegating orchestrator.
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
        if not bets:
            return []

        # --- Regime multipliers per sport in the batch (cached for this call) ---
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
        portfolio_bets, sized = build_portfolio_requests(bets, correlation_matrix)

        results: list[dict] = []
        for i, item in enumerate(sized):
            b = bets[i]
            frac = float(item.get("final_fraction", 0.0))
            signals_n = int(b.get("signals_n", 0) or 0)
            kelly_frac = self._signals_n_to_kelly_fraction(signals_n)
            scale = (kelly_frac / KELLY_FRACTION) if KELLY_FRACTION > 0 else 1.0
            frac = frac * scale
            stake_before_regime = round(bankroll * frac, 2)
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

        # Second/third passes: per-game + per-sport caps, then min-bet floor.
        results = apply_exposure_caps(results, bankroll)

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
        selection_text = betexec_slip.build_selection_text(market, team, side, point)

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

        status = evaluate_drawdown(current, peak)

        if not status["triggered"]:
            return status

        # Kill switch fires.
        logger.error(
            f"DRAWDOWN KILL SWITCH: current=${current:,.2f} peak=${peak:,.2f} "
            f"drawdown={status['drawdown_pct']:.1%} exceeds threshold {MAX_DRAWDOWN_PCT:.1%}"
        )
        self._enabled = False

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
            msg = build_kill_switch_alert(
                current, peak, status["drawdown_pct"], len(status["paused_hypotheses"])
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
        await betexec_logging.log_action(
            self._db, action, sport, team, market, side, odds, stake, edge,
            hypothesis_id, bet_id=bet_id, screenshot=screenshot, reason=reason,
        )

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
            self._context = None
            self._page = None
        if self._db:
            await self._db.close()
            self._db = None
        self._enabled = False
        self._logged_in = False
        logger.info("Bet executor shut down")
