"""Pairwise hypothesis correlation matrix extracted from tools.auto.research.

``CorrelationMixin`` builds a Jaccard co-firing matrix from
``backtest_events`` history and a ``signals_n`` map for the
portfolio/Kelly layer. Cached with TTL ``CALLISTO_CORR_TTL_SECONDS``.
Re-exported from tools.auto.research so ResearchLoop composition and
slice3 hasattr pins stay intact.

Do not import tools.autonomous. Do not arm live betting.
Do not add live to paper-signal.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("callisto.auto.research")


class CorrelationMixin:

    async def _build_correlation_matrix(
        self, hypothesis_ids: list[str], lookback_days: int = 30
    ) -> dict[tuple[str, str], float]:
        """Build a pairwise correlation matrix from ``backtest_events`` history.

        For each pair (A, B), compute
            corr(A, B) = |events where A AND B signalled on same event_id| /
                         |events where A OR B signalled|
        over the last ``lookback_days``. This is the Jaccard co-firing rate —
        a conservative proxy for bet correlation when both sit on the same
        event. Perfect co-firing = 1.0, no overlap = 0.0.

        Cached on ``self._corr_matrix_cache`` with TTL
        ``CALLISTO_CORR_TTL_SECONDS`` (default 4h). The cache is keyed by
        the sorted tuple of hypothesis_ids so demotion/promotion invalidates
        it implicitly.
        """
        cache_ttl = int(os.getenv("CALLISTO_CORR_TTL_SECONDS", "14400"))
        cache_key = tuple(sorted(hypothesis_ids))
        cache = getattr(self, "_corr_matrix_cache", {})
        now_ts = time.time()
        if cache_key in cache:
            cached_at, matrix = cache[cache_key]
            if now_ts - cached_at < cache_ttl:
                return matrix

        db = self.data_collector._db if self.data_collector else None
        if not db or not hypothesis_ids:
            return {}

        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

        # Pull (hypothesis_id, event_id) tuples where signal_generated=1 in window.
        try:
            placeholders = ",".join(["?"] * len(hypothesis_ids))
            cursor = await db.execute(
                f"SELECT hypothesis_id, event_id FROM backtest_events "
                f"WHERE signal_generated = 1 AND hypothesis_id IN ({placeholders}) "
                f"AND created_at >= ?",
                (*hypothesis_ids, since),
            )
            rows = await cursor.fetchall()
        except Exception as e:
            logger.warning(f"Correlation matrix: query failed: {e}")
            return {}

        # Build per-hyp event_id sets.
        fired: dict[str, set[str]] = {}
        for hid, eid in rows:
            if not eid:
                continue
            fired.setdefault(hid, set()).add(eid)

        matrix: dict[tuple[str, str], float] = {}
        ids = sorted(hypothesis_ids)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                sa = fired.get(a, set())
                sb = fired.get(b, set())
                union = len(sa | sb)
                if union == 0:
                    corr = 0.0
                else:
                    corr = len(sa & sb) / union
                matrix[(a, b)] = round(corr, 4)

        # Store with timestamp; cap cache growth.
        cache[cache_key] = (now_ts, matrix)
        if len(cache) > 32:
            oldest = min(cache, key=lambda k: cache[k][0])
            cache.pop(oldest, None)
        self._corr_matrix_cache = cache
        return matrix

    async def _hyp_signals_n_map(self, hypothesis_ids: list[str]) -> dict[str, int]:
        """Return {hypothesis_id: most_recent_signals_n} from hypothesis_stats."""
        db = self.data_collector._db if self.data_collector else None
        if not db or not hypothesis_ids:
            return {}
        placeholders = ",".join(["?"] * len(hypothesis_ids))
        try:
            cursor = await db.execute(
                f"SELECT hypothesis_id, signals_n FROM hypothesis_stats "
                f"WHERE hypothesis_id IN ({placeholders}) "
                f"ORDER BY computed_at DESC",
                tuple(hypothesis_ids),
            )
            rows = await cursor.fetchall()
        except Exception:
            return {}
        result: dict[str, int] = {}
        for hid, n in rows:
            if hid not in result:
                result[hid] = int(n or 0)
        return result
