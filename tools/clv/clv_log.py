"""CLV log writers — the permanent record of signal quality."""

import logging
from datetime import datetime, timezone
from typing import Optional

from tools.book_keys import canonicalize_book

from tools.clv.constants import (
    BOOK_VIG_ESTIMATE,
    DEFAULT_CLOSING_VIG,
    DEFAULT_PLACEMENT_VIG,
    RELIABLE_CLOSE_SOURCES,
)
from tools.clv.odds_math import american_to_decimal, half_vig_devig, regime_stamp

logger = logging.getLogger("callisto.clv_tracker")

_CLV_LOG_INSERT_SQL = (
    "INSERT OR REPLACE INTO clv_log "
    "(bet_id, event, outcome, point, book, our_odds_decimal, "
    "pinnacle_close_fair_prob, pinnacle_close_fair_decimal, "
    "clv_cents, clv_prob_bp, actual_result, actual_pnl, "
    "close_reliable, logged_at, regime_phase_at_placement) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class CLVLogMixin:
    """Mixin for CLVTracker: writes to the append-only ``clv_log`` table.

    Requires the host class to provide ``self._db`` (aiosqlite connection).
    """

    async def _log_clv(
        self,
        bet: dict,
        result: str,
        payout: Optional[float],
        change: float,
    ) -> None:
        """
        Write CLV metrics to the clv_log table.

        This is called on every bet resolution. The clv_log is the permanent,
        append-only record of signal quality. If we consistently show positive
        CLV, the edge is real regardless of short-term variance.
        """
        bet_id = bet.get("id")
        placement_odds = bet.get("placement_odds")
        closing_odds = bet.get("closing_odds")
        closing_implied = bet.get("closing_implied_prob")

        our_decimal = american_to_decimal(placement_odds)

        # Canonicalize books BEFORE any lookup — 'Betfair Exchange',
        # 'betfair exchange', 'betfair_exchange' must all collapse onto
        # one key. Previously `closing_source.lower()` left spaces
        # intact and dropped matches with the odds-api.io underscore form.
        closing_source = canonicalize_book(bet.get("closing_source") or "pinnacle")
        bookmaker = canonicalize_book(bet.get("bookmaker") or "")
        closing_vig = BOOK_VIG_ESTIMATE.get(closing_source, DEFAULT_CLOSING_VIG)
        placement_vig = BOOK_VIG_ESTIMATE.get(bookmaker, DEFAULT_PLACEMENT_VIG)

        pinnacle_fair_prob = half_vig_devig(closing_implied, closing_vig)
        pinnacle_fair_decimal = None
        if pinnacle_fair_prob and pinnacle_fair_prob > 0:
            pinnacle_fair_decimal = 1 / pinnacle_fair_prob

        # CANONICAL CLV UNIT: probability-basis-points.
        #   clv_prob_bp = (closing_implied_prob - placement_implied_prob) * 10000
        # Positive = we got a better implied price than the close.
        # This is the ONLY supported unit going forward. The legacy
        # clv_cents column is preserved for backward-compat readers but
        # marked deprecated — it previously held American-point deltas
        # on one path (line 414) and prob×10000 on another (line 419),
        # producing a column with mixed units that silently poisoned
        # every aggregate.
        raw_placement_implied = bet.get("placement_implied_prob", 0)
        placement_fair = (
            half_vig_devig(raw_placement_implied, placement_vig)
            if raw_placement_implied else None
        )

        clv_prob_bp = None
        if pinnacle_fair_prob is not None and placement_fair is not None:
            clv_prob_bp = round((pinnacle_fair_prob - placement_fair) * 10000, 1)

        # Legacy clv_cents: populate with prob-bp for NEW rows so mixed-units
        # data stops accruing. Historical rows keep whatever they have.
        clv_cents = clv_prob_bp

        actual_pnl = change
        close_reliable = (
            closing_odds is not None
            and closing_source in RELIABLE_CLOSE_SOURCES
        )

        now = datetime.now(timezone.utc).isoformat()

        # Regime stamp at placement (feat/regime-aware-sizing, 2026-04-22).
        # Format: "<sport>|<season_phase>" — compact so downstream GROUP BY
        # queries can split on '|' to get both dimensions. Errors degrade
        # to None (no stamp) so clv_log writes never fail on regime lookup.
        regime_stamp_value = regime_stamp(bet.get("sport", ""))

        try:
            from tools.db_utils import execute_with_retry
            await execute_with_retry(
                self._db,
                _CLV_LOG_INSERT_SQL,
                (
                    str(bet_id),
                    bet.get("event_id", ""),
                    bet.get("team", ""),
                    bet.get("placement_point"),
                    # Store canonicalized bookmaker so downstream group-by
                    # queries don't fragment on casing.
                    canonicalize_book(bet.get("bookmaker", "")),
                    our_decimal,
                    pinnacle_fair_prob,
                    pinnacle_fair_decimal,
                    clv_cents,
                    clv_prob_bp,
                    result,
                    actual_pnl,
                    close_reliable,
                    now,
                    regime_stamp_value,
                ),
                max_retries=10,
                operation="clv_tracker log_clv",
            )
            logger.info(
                f"CLV logged: bet #{bet_id}, clv_prob_bp={clv_prob_bp}, "
                f"result={result}, pnl={actual_pnl}, reliable={close_reliable}, "
                f"regime={regime_stamp_value}"
            )
        except Exception as e:
            logger.warning(f"Failed to log CLV for bet #{bet_id}: {e}")

    async def log_paper_trade_clv(self, trade: dict) -> bool:
        """Write a resolved paper trade to ``clv_log`` — same schema as real bets.

        Paper trades are Callisto's only bet-like data while the real executor
        is disabled. Without this entry, the clv_log — our "permanent record
        of signal quality" — stays empty and every promotion gate that
        consults it has nothing to grade. bet_id is namespaced ``pt:<trade_id>``
        so paper IDs never collide with real bet integer ids.

        Returns True if a row was written, False if the trade lacked the
        minimum inputs (no signal implied prob, or no actual_result).
        """
        if not trade.get("actual_result"):
            return False
        signal_imp = trade.get("signal_implied_prob")
        if signal_imp is None:
            return False

        trade_id = trade.get("trade_id")
        if not trade_id:
            return False
        bet_key = f"pt:{trade_id}"

        signal_odds = trade.get("signal_odds_american")
        closing_odds = trade.get("closing_odds")
        closing_implied = trade.get("closing_implied")
        bookmaker = canonicalize_book(trade.get("book") or "")

        our_decimal = american_to_decimal(signal_odds)

        # Paper trades don't store which book supplied the close — the
        # backfill in data_collector prefers Pinnacle/LowVig, so treat
        # closing_implied as already close to fair and use a sharp-tier vig.
        placement_vig = BOOK_VIG_ESTIMATE.get(bookmaker, DEFAULT_PLACEMENT_VIG)
        closing_vig = DEFAULT_CLOSING_VIG

        signal_fair = half_vig_devig(signal_imp, placement_vig)
        close_fair = half_vig_devig(closing_implied, closing_vig)
        close_fair_decimal = (1 / close_fair) if close_fair and close_fair > 0 else None

        # CANONICAL CLV UNIT: prob-basis-points. See _log_clv for rationale.
        # The legacy American-points path (signal_odds - closing_odds) is
        # intentionally removed: it made clv_cents incompatible with the
        # other write path and silently poisoned every aggregate that
        # grouped over bet_id.
        clv_prob_bp = None
        if close_fair is not None and signal_fair is not None:
            clv_prob_bp = round((close_fair - signal_fair) * 10000, 1)
        clv_cents = clv_prob_bp

        # We don't know the closing source for paper trades, only that the
        # backfill prefers sharp books when available. Mark reliable iff a
        # closing number was actually matched — this mirrors how downstream
        # queries treat "unknown but present" close data.
        close_reliable = closing_odds is not None or closing_implied is not None

        now = datetime.now(timezone.utc).isoformat()

        regime_stamp_value = regime_stamp(trade.get("sport", ""))

        try:
            from tools.db_utils import execute_with_retry
            await execute_with_retry(
                self._db,
                _CLV_LOG_INSERT_SQL,
                (
                    bet_key,
                    trade.get("event_id", ""),
                    trade.get("side", ""),
                    trade.get("line"),
                    bookmaker,
                    our_decimal,
                    close_fair,
                    close_fair_decimal,
                    clv_cents,
                    clv_prob_bp,
                    trade.get("actual_result"),
                    trade.get("hypothetical_pnl"),
                    close_reliable,
                    now,
                    regime_stamp_value,
                ),
                max_retries=10,
                operation="clv_tracker log_paper_trade_clv",
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to log paper-trade CLV for {trade_id}: {e}")
            return False

    async def sync_paper_trades_to_clv_log(self, limit: Optional[int] = None) -> int:
        """Idempotent: copy every resolved paper_trade missing from clv_log.

        Finds rows where ``actual_result`` is populated but the corresponding
        ``clv_log`` entry (``pt:<trade_id>``) doesn't exist. Safe to call on
        every resolution pass — the anti-join skips already-logged trades and
        the INSERT uses INSERT OR REPLACE as a final safeguard.

        Returns the count of clv_log rows written.
        """
        sql = (
            "SELECT pt.* FROM paper_trades pt "
            "WHERE pt.actual_result IS NOT NULL "
            "AND pt.signal_implied_prob IS NOT NULL "
            "AND NOT EXISTS ("
            "    SELECT 1 FROM clv_log cl WHERE cl.bet_id = 'pt:' || pt.trade_id"
            ")"
        )
        if limit and limit > 0:
            sql += f" LIMIT {int(limit)}"

        cursor = await self._db.execute(sql)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]

        written = 0
        for row in rows:
            trade = dict(zip(cols, row))
            if await self.log_paper_trade_clv(trade):
                written += 1

        if written > 0:
            from tools.db_utils import commit_with_retry
            await commit_with_retry(
                self._db,
                max_retries=10,
                operation="clv_tracker sync_paper_trades_to_clv_log",
            )
            logger.info(
                f"clv_log sync: wrote {written} paper-trade entries "
                f"(of {len(rows)} candidates)"
            )
        return written

    async def backfill_clv_log(self) -> int:
        """
        Backfill clv_log for all resolved bets that don't have entries.

        Call this once to populate clv_log from existing bets data.
        Safe to call multiple times (INSERT OR REPLACE).
        """
        cursor = await self._db.execute(
            "SELECT * FROM bets WHERE result IN ('won', 'lost', 'push')"
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]

        count = 0
        for row in rows:
            bet = dict(zip(cols, row))
            result = bet["result"]
            payout = bet.get("payout")
            stake = bet.get("stake", 100)
            if result == "won" and payout:
                change = payout - stake
            elif result == "lost":
                change = -stake
            else:
                change = 0
            await self._log_clv(bet, result, payout, change)
            count += 1

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, max_retries=10, operation="clv_tracker backfill_clv_log")

        # Paper trades are the dominant source of bet-like data today (real
        # executor is off). Sweep them in the same pass so a single call to
        # backfill_clv_log leaves clv_log fully caught up.
        paper_written = await self.sync_paper_trades_to_clv_log()

        logger.info(
            f"Backfilled CLV log: {count} real bets, {paper_written} paper trades"
        )
        return count + paper_written
