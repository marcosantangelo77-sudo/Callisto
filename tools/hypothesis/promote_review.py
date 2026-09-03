"""LIVE-stage review extracted from tools.hypothesis.promote.

``HypothesisPromotionMixin.review_live_hypotheses`` stays defined on the
mixin as a thin delegate so ``hasattr`` pins keep passing. The review
body lives here so ``tools/hypothesis/promote.py`` can keep shrinking
without changing behaviour.

``auto_promote`` stays in promote.py (diagnose-only; no evidence rewrite).
``check_promotion_readiness`` stays on ``HypothesisSignificanceMixin`` —
do not copy it onto ``HypothesisPromotionMixin``.

This reviews hypotheses already in live status. It does not arm live
betting and does not add ``live`` to paper-signal statuses.

Do not import tools.autonomous.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools.resolvers.base_rates import (
    base_rate_relative_floor,
    expected_base_rate_from_events,
)
from tools.hypothesis.config import LIVE_REVIEW_WINDOW_DAYS

logger = logging.getLogger("callisto.hypothesis")


async def review_live_hypotheses(
    self,
    *,
    window_days: Optional[int] = None,
    hit_rate_floor: float = 0.45,
    max_drawdown: float = 0.40,
    min_resolved: int = 10,
    clv_negative_threshold: float = 0.0,
    base_rate_relative: bool = True,
) -> list[dict]:
    """Review all LIVE hypotheses and demote underperformers to 'paused'.

    Pulls the trailing `window_days` of resolved bets from paper_trades
    (and clv_log as supplementary CLV evidence), computes rolling hit-rate,
    ROI, Sharpe, and max drawdown, and demotes when:

      * hit_rate < effective_floor      (sub-prior performance)
      * max_drawdown > max_drawdown     (excessive drawdown)
      * avg CLV < clv_negative_threshold (betting bad prices)

    Effective floor: when base_rate_relative=True (default), each
    hypothesis's hit-rate floor is derived from its own expected base
    rate (mean book implied probability of its trades) via
    tools.resolvers.base_rates.base_rate_relative_floor; the
    ``hit_rate_floor`` argument then acts only as the legacy ceiling.
    Low-base-rate claims are judged against their own prior, not the
    50%-domain constant.

    Returns a list of per-hypothesis outcome dicts.
    """
    from tools.math_utils import american_to_decimal
    from tools.market_microstructure import sortino_ratio as _sortino

    window = window_days if window_days is not None else LIVE_REVIEW_WINDOW_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()

    live_rows = await self.list_hypotheses(status="live")
    results: list[dict] = []

    for h in live_rows:
        hid = h["hypothesis_id"]
        # ── Pull resolved bets within window from paper_trades ──
        trade_cur = await self._db.execute(
            "SELECT signal_odds_american, actual_result, clv_implied, "
            "signal_implied_prob, game_date "
            "FROM paper_trades "
            "WHERE hypothesis_id = ? "
            "  AND actual_result IN ('won','lost','push') "
            "  AND (created_at >= ? OR game_date >= ?) "
            "ORDER BY game_date",
            (hid, cutoff, cutoff[:10]),
        )
        rows = await trade_cur.fetchall()

        # Supplementary CLV from clv_log (the signal-quality ledger). We
        # don't require a hypothesis_id match here — clv_log is not always
        # tagged — so this is a best-effort supplement only.
        clv_values: list[float] = []
        for row in rows:
            clv = row[2]
            imp = row[3]
            if clv is not None and imp is not None:
                # Positive = model priced above close (got the better price).
                clv_values.append(float(clv) - float(imp))

        returns: list[float] = []
        wins = losses = pushes = 0
        for odds_american, actual_result, _clv, _imp, _gd in rows:
            if actual_result == "won":
                try:
                    dec = american_to_decimal(int(odds_american))
                    returns.append(dec - 1.0)
                except Exception:
                    returns.append(0.0)
                wins += 1
            elif actual_result == "lost":
                returns.append(-1.0)
                losses += 1
            elif actual_result == "push":
                returns.append(0.0)
                pushes += 1

        n_resolved = wins + losses  # pushes don't count toward hit rate
        hit_rate = wins / n_resolved if n_resolved else 0.0
        roi = sum(returns) / len(returns) if returns else 0.0
        # Drawdown
        mdd = 0.0
        if returns:
            equity = 0.0
            peak = 0.0
            for r in returns:
                equity += r
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / (abs(peak) + 1.0)
                if dd > mdd:
                    mdd = dd
        sortino = _sortino(returns) if returns else None
        avg_clv = sum(clv_values) / len(clv_values) if clv_values else None

        outcome: dict = {
            "hypothesis_id": hid,
            "name": h.get("name"),
            "window_days": window,
            "n_resolved": n_resolved,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "hit_rate": round(hit_rate, 4),
            "roi": round(roi, 4),
            "max_drawdown": round(mdd, 4),
            "sortino": round(sortino, 4) if sortino is not None else None,
            "avg_clv": round(avg_clv, 4) if avg_clv is not None else None,
            "demoted": False,
            "reasons": [],
        }

        # Don't demote with insufficient data — a 2-game losing streak
        # should not pause a freshly-promoted hypothesis.
        if n_resolved < min_resolved:
            outcome["decision"] = "hold_insufficient_sample"
            results.append(outcome)
            continue

        reasons = []
        # Base-rate-relative effective floor (B1): judge the claim
        # against its own prior. Unknown base rate → legacy floor.
        _eff_floor = hit_rate_floor
        if base_rate_relative:
            _base = expected_base_rate_from_events(
                [{"book_implied_prob": imp} for (_o, _r, _c, imp, _g) in rows
                 if imp is not None]
            )
            _eff_floor = base_rate_relative_floor(
                _base, legacy_floor=hit_rate_floor
            )
            outcome["effective_hit_rate_floor"] = round(_eff_floor, 4)
            outcome["expected_base_rate"] = (
                round(_base, 4) if _base is not None else None
            )
        if hit_rate < _eff_floor:
            reasons.append(
                f"hit_rate {hit_rate:.1%} < {_eff_floor:.0%} floor"
                + (f" (base-rate-relative; prior={_base:.0%})" if base_rate_relative and _base is not None else "")
            )
        if mdd > max_drawdown:
            reasons.append(
                f"drawdown {mdd:.1%} > {max_drawdown:.0%} threshold"
            )
        if avg_clv is not None and avg_clv < clv_negative_threshold:
            reasons.append(
                f"avg CLV {avg_clv:.4f} < {clv_negative_threshold}"
            )

        if not reasons:
            outcome["decision"] = "hold_healthy"
            results.append(outcome)
            continue

        # Demote → 'paused'. CAS on 'live' so a concurrent retirement
        # doesn't race us.
        reason_str = "auto:live_underperform — " + "; ".join(reasons)
        cas = await self.update_status(
            hid, "paused", reason_str, expected_status="live",
        )
        outcome["reasons"] = reasons
        outcome["demoted"] = bool(cas.get("changed"))
        outcome["decision"] = (
            "demoted_to_paused" if cas.get("changed") else "cas_noop"
        )

        # Log the demotion into hypothesis_stats for visibility. Best-effort
        # — we don't block demotion on the logging call.
        try:
            from tools.db_utils import execute_with_retry, commit_with_retry
            await execute_with_retry(
                self._db,
                "INSERT INTO hypothesis_stats "
                "(hypothesis_id, stage, computed_at, total_n, signals_n, "
                " win, loss, push_, hit_rate, avg_clv, "
                " positive_clv_rate, roi_pct, max_drawdown, sortino, is_significant) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    hid,
                    "live_review",
                    datetime.now(timezone.utc).isoformat(),
                    n_resolved + pushes,
                    n_resolved,
                    wins,
                    losses,
                    pushes,
                    hit_rate,
                    avg_clv,
                    None,
                    roi * 100.0,
                    mdd,
                    sortino,
                    False,
                ),
                operation="live_review insert stats",
            )
            await commit_with_retry(self._db, operation="live_review stats")
        except Exception as e:
            logger.warning(f"live_review stats insert failed for {hid}: {e}")

        # Attach a wiki article so demotions appear in the research trail.
        # Pre-2026-04-22: this used (article_id, title, body, domain,
        # created_at) — a schema that NEVER existed. The INSERT raised
        # every call, was swallowed by `except Exception: pass`, and not
        # a single demotion ever made it into the wiki. Now routed through
        # knowledge_wiki.write_lesson_article which uses the real schema
        # (topic PK, title, content, summary, related_topics, ...) and
        # increments _wiki_writes_failed on any real error so we notice.
        try:
            from tools.knowledge_wiki import get_wiki
            wiki = get_wiki()
            demotion_content = (
                f"LIVE demotion post-mortem for {h.get('name', hid)} "
                f"(hypothesis_id={hid}).\n\n"
                f"Reason: {reason_str}\n\n"
                f"Performance window (last {window} days):\n"
                f"  - n_resolved: {n_resolved}\n"
                f"  - hit_rate: {hit_rate:.1%}\n"
                f"  - roi: {roi:.2%}\n"
                f"  - max_drawdown: {mdd:.1%}\n"
                f"  - avg_clv: {avg_clv}\n"
                f"  - sortino: {sortino}\n\n"
                f"Sport: {h.get('sport', 'unknown')}\n"
                f"Market: {h.get('market_type', 'unknown')}\n"
                f"Demoted at: {datetime.now(timezone.utc).isoformat()}\n\n"
                f"This article is retrievable by the hypothesis generator "
                f"so similar patterns in the same cohort aren't "
                f"re-proposed without acknowledging this prior failure."
            )
            topic_slug = f"{hid}_live_demotion_lessons"
            write_result = await wiki.write_lesson_article(
                self._db,
                topic=topic_slug,
                title=f"LIVE demotion: {h.get('name', hid)}",
                content=demotion_content,
                domain="SIGNAL",
                related_topics=[
                    "demotion_lessons",
                    f"sport:{h.get('sport', 'unknown')}",
                    f"market:{h.get('market_type', 'unknown')}",
                    "live_review_failure",
                ],
                confidence=0.7,
            )
            outcome["wiki_article_topic"] = topic_slug
            outcome["wiki_write_action"] = write_result.get("action")
            if write_result.get("action") == "failed":
                # Loud, not silent — the whole point of this fix.
                logger.warning(
                    f"Demotion wiki write FAILED for {hid}: "
                    f"{write_result.get('error')}"
                )
        except Exception as e:
            # Explicit log + counter bump — replaces the old bare `pass`.
            logger.warning(
                f"Demotion wiki lesson write raised for {hid}: "
                f"{type(e).__name__}: {e}"
            )
            try:
                from tools import knowledge_wiki as _kw
                _kw._wiki_writes_failed += 1
            except Exception:
                pass

        results.append(outcome)

    return results
