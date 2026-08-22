"""Betting resolver — the first OutcomeResolver implementation.

Reads the existing sports tables (backtest_events, paper_trades, clv_log)
and adapts them onto EvidenceRecord. This is a pure adapter: no behavior of
the existing pipeline changes by its existence; callers opt in.

CLV note: the canonical devigged statistic is clv_log.clv_prob_bp
(basis points of devigged probability between placement and close). This
resolver prefers it when joining on paper trades (bet_id 'pt:<trade_id>'),
falling back to paper_trades.clv_implied deltas when clv_log has no entry.
"""

from __future__ import annotations

from typing import AsyncIterator

import aiosqlite

from tools.resolvers.base import EvidenceRecord, OutcomeResolver


class BettingOutcomeResolver(OutcomeResolver):
    name = "betting"

    def __init__(self, db: aiosqlite.Connection, *, include_clv_log: bool = True):
        self._db = db
        self._include_clv_log = include_clv_log

    async def iter_evidence(
        self, hypothesis_id: str, stage: str = "paper_trading"
    ) -> AsyncIterator[EvidenceRecord]:
        """Yield evidence for a hypothesis.

        stage='backtesting' reads backtest_events; anything else reads
        paper_trades (preregistered forward tests) with canonical CLV joined
        from clv_log where available.
        """
        if stage == "backtesting":
            async for rec in self._iter_backtest(hypothesis_id):
                yield rec
        else:
            async for rec in self._iter_paper(hypothesis_id):
                yield rec

    async def _iter_backtest(self, hypothesis_id: str) -> AsyncIterator[EvidenceRecord]:
        cur = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE hypothesis_id = ? AND actual_result IN ('won','lost','push')",
            (hypothesis_id,),
        )
        cols = [d[0] for d in cur.description]
        for row in await cur.fetchall():
            yield EvidenceRecord.from_betting_row(dict(zip(cols, row)), source=self.name)

    async def _iter_paper(self, hypothesis_id: str) -> AsyncIterator[EvidenceRecord]:
        cur = await self._db.execute(
            "SELECT pt.*, cl.clv_prob_bp AS canon_clv_prob_bp "
            "FROM paper_trades pt "
            "LEFT JOIN clv_log cl ON cl.bet_id = 'pt:' || pt.trade_id "
            "WHERE pt.hypothesis_id = ? AND pt.actual_result IN ('won','lost','push')",
            (hypothesis_id,),
        )
        if not self._include_clv_log:
            cur = await self._db.execute(
                "SELECT NULL AS canon_clv_prob_bp, pt.* FROM paper_trades pt "
                "WHERE pt.hypothesis_id = ? AND pt.actual_result IN ('won','lost','push')",
                (hypothesis_id,),
            )
        cols = [d[0] for d in cur.description]
        rows = await cur.fetchall()
        for row in rows:
            d = dict(zip(cols, row))
            # Canonical devigged CLV wins over the raw implied delta.
            if d.get("canon_clv_prob_bp") is not None:
                d["clv_prob_bp"] = d["canon_clv_prob_bp"]
            yield EvidenceRecord.from_betting_row(d, source=self.name)

    async def mean_clv_prob_bp(self, hypothesis_id: str) -> tuple[float | None, int]:
        """Canonical gate statistic: mean devigged CLV in basis points over
        resolved forward-tests, plus the sample size. None when no data."""
        try:
            cur = await self._db.execute(
                "SELECT AVG(cl.clv_prob_bp), COUNT(cl.clv_prob_bp) "
                "FROM paper_trades pt "
                "JOIN clv_log cl ON cl.bet_id = 'pt:' || pt.trade_id "
                "WHERE pt.hypothesis_id = ? "
                "  AND pt.actual_result IN ('won','lost','push') "
                "  AND cl.clv_prob_bp IS NOT NULL",
                (hypothesis_id,),
            )
            row = await cur.fetchone()
        except Exception:
            return None, 0
        if not row or row[0] is None:
            return None, int(row[1]) if row else 0
        return float(row[0]), int(row[1])
