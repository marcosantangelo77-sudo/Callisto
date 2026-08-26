"""Section builders for Hermes memory context (bets, edges, patterns, active,
research, learnings, messages, code changes).

Extracted verbatim from tools/hermes_memory.py during the tools.hmem split.
"""

import logging
import os
import subprocess
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger("callisto.hermes")


async def build_bet_history(db: aiosqlite.Connection) -> str:
    """Bet outcomes and bankroll state."""
    bal_row = await db.execute_fetchall(
        "SELECT balance FROM bankroll ORDER BY timestamp DESC LIMIT 1"
    )
    bankroll = bal_row[0][0] if bal_row else "unknown"

    total_row = await db.execute_fetchall("SELECT COUNT(*) FROM bets")
    total_bets = total_row[0][0] if total_row else 0

    won_row = await db.execute_fetchall("SELECT COUNT(*) FROM bets WHERE result='won'")
    lost_row = await db.execute_fetchall("SELECT COUNT(*) FROM bets WHERE result='lost'")
    pending_row = await db.execute_fetchall("SELECT COUNT(*) FROM bets WHERE result='pending'")
    won = won_row[0][0] if won_row else 0
    lost = lost_row[0][0] if lost_row else 0
    pending = pending_row[0][0] if pending_row else 0

    pl_row = await db.execute_fetchall(
        "SELECT COALESCE(SUM(CASE WHEN result='won' THEN payout - stake "
        "WHEN result='lost' THEN -stake ELSE 0 END), 0) FROM bets"
    )
    pl = pl_row[0][0] if pl_row else 0

    clv_row = await db.execute_fetchall(
        "SELECT AVG(clv_implied) FROM bets WHERE clv_implied IS NOT NULL"
    )
    avg_clv = clv_row[0][0] if clv_row and clv_row[0][0] is not None else None

    recent = await db.execute_fetchall(
        "SELECT game_description, team, market, placement_odds, result, "
        "stake, payout, clv_implied FROM bets ORDER BY placed_at DESC LIMIT 5"
    )

    lines = ["<memory type=\"bets\">"]
    lines.append(f"Bankroll: ${bankroll}")
    lines.append(f"Record: {won}W-{lost}L ({pending} pending) | Total: {total_bets}")
    if won + lost > 0:
        lines.append(f"Win rate: {won/(won+lost)*100:.0f}% | P/L: ${pl:+.2f}")
    if avg_clv is not None:
        direction = "BEATING" if avg_clv > 0 else "BEHIND"
        lines.append(f"Avg CLV: {avg_clv:+.2%} ({direction} closing lines)")

    if recent:
        lines.append("Recent:")
        for r in recent:
            game, team, market, odds, result, stake, payout, clv = r
            odds_str = f"+{odds}" if odds > 0 else str(odds)
            status = result.upper() if result != 'pending' else 'OPEN'
            line = f"  {status}: {team} {market} {odds_str}"
            if result == 'won' and payout:
                line += f" \u2192 +${payout - stake:.0f}"
            elif result == 'lost':
                line += f" \u2192 -${stake:.0f}"
            if clv is not None:
                line += f" (CLV: {clv:+.2%})"
            lines.append(line)

    lines.append("</memory>")
    return "\n".join(lines)


async def build_edge_history(db: aiosqlite.Connection) -> str:
    """Recent edge detection results."""
    ev_opps = await db.execute_fetchall(
        "SELECT sport, team, market, bookmaker, american_odds, edge, "
        "expected_value, kelly_fraction, detected_at "
        "FROM ev_opportunities ORDER BY detected_at DESC LIMIT 5"
    )

    sessions = await db.execute_fetchall(
        "SELECT query, conclusion, confidence_score, confidence_tier, sealed_at "
        "FROM sessions WHERE query LIKE '%AUTONOMOUS%' OR query LIKE '%edge%' "
        "ORDER BY sealed_at DESC LIMIT 3"
    )

    if not sessions and not ev_opps:
        return ""

    lines = ["<memory type=\"edges\">"]

    if ev_opps:
        lines.append("Recent +EV opportunities:")
        for o in ev_opps:
            sport, team, market, book, odds, edge, ev, kelly, detected = o
            odds_str = f"+{odds}" if odds > 0 else str(odds)
            lines.append(
                f"  {team} {market} @ {odds_str} ({book}) | "
                f"edge={edge:.1%}, EV=${ev:.2f}, Kelly={kelly:.1%} [{detected[:10]}]"
            )

    if sessions:
        lines.append("Recent analysis:")
        for s in sessions:
            query, conclusion, conf, tier, sealed = s
            q_short = query[:60].replace("\n", " ")
            c_short = (conclusion or "")[:80].replace("\n", " ")
            lines.append(f"  [{tier} {conf:.2f}] {q_short}... \u2192 {c_short}")

    lines.append("</memory>")
    return "\n".join(lines)


async def build_learned_patterns(db: aiosqlite.Connection) -> str:
    """Patterns from EV data + explicit learnings from operation."""
    market_stats = await db.execute_fetchall(
        "SELECT market, COUNT(*) as cnt, AVG(edge) as avg_edge "
        "FROM ev_opportunities GROUP BY market ORDER BY cnt DESC LIMIT 5"
    )

    book_stats = await db.execute_fetchall(
        "SELECT bookmaker, COUNT(*) as cnt, AVG(edge) as avg_edge "
        "FROM ev_opportunities GROUP BY bookmaker ORDER BY cnt DESC LIMIT 5"
    )

    market_perf = await db.execute_fetchall(
        "SELECT market, "
        "SUM(CASE WHEN result='won' THEN 1 ELSE 0 END) as wins, "
        "SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END) as losses "
        "FROM bets WHERE result IN ('won', 'lost') "
        "GROUP BY market"
    )

    if not market_stats and not book_stats and not market_perf:
        return ""

    lines = ["<memory type=\"patterns\">"]

    if market_stats:
        lines.append("Edge frequency by market:")
        for m in market_stats:
            lines.append(f"  {m[0]}: {m[1]} edges found, avg {m[2]:.1%}")

    if book_stats:
        lines.append("Edge frequency by book:")
        for b in book_stats:
            lines.append(f"  {b[0]}: {b[1]} edges, avg {b[2]:.1%}")

    if market_perf:
        lines.append("Bet performance by market:")
        for mp in market_perf:
            market, wins, losses = mp
            total = wins + losses
            if total > 0:
                lines.append(f"  {market}: {wins}W-{losses}L ({wins/total*100:.0f}%)")

    lines.append("</memory>")
    return "\n".join(lines)


async def build_active_state(db: aiosqlite.Connection) -> str:
    """Current open bets."""
    open_bets = await db.execute_fetchall(
        "SELECT id, game_description, team, market, bookmaker, "
        "placement_odds, stake, notes FROM bets WHERE result='pending' "
        "ORDER BY placed_at DESC"
    )

    if not open_bets:
        return ""

    lines = ["<memory type=\"active\">"]
    lines.append(f"Open bets ({len(open_bets)}):")
    for b in open_bets:
        bid, game, team, market, book, odds, stake, notes = b
        odds_str = f"+{odds}" if odds > 0 else str(odds)
        lines.append(f"  Bet #{bid}: {team} {market} {odds_str} (${stake} @ {book})")
        if notes:
            lines.append(f"    {notes[:60]}")

    lines.append("</memory>")
    return "\n".join(lines)


async def build_research_state(db: aiosqlite.Connection) -> str:
    """Hypothesis testing state — what's been tried, what's promising."""
    try:
        status_rows = await db.execute_fetchall(
            "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
        )
        status_counts = {r[0]: r[1] for r in status_rows}

        top_hypos = await db.execute_fetchall(
            """SELECT h.name, h.sport, h.market_type, h.thesis,
                      COUNT(DISTINCT be.event_id) as events,
                      SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as signals,
                      AVG(be.edge) as avg_edge
               FROM hypotheses h
               LEFT JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id
               WHERE h.status IN ('backtesting', 'paper_trading', 'live', 'draft')
               GROUP BY h.hypothesis_id
               ORDER BY signals DESC, events DESC
               LIMIT 25"""
        )

        rejected = await db.execute_fetchall(
            "SELECT name, thesis FROM hypotheses WHERE status='rejected' "
            "ORDER BY updated_at DESC LIMIT 3"
        )

        lines = ["<memory type=\"research\">"]
        total = sum(status_counts.values())
        lines.append(f"Hypotheses: {total} total")
        for s in ['draft', 'backtesting', 'paper_trading', 'live', 'rejected']:
            if status_counts.get(s, 0) > 0:
                lines.append(f"  {s}: {status_counts[s]}")

        if top_hypos:
            lines.append("Most tested:")
            for h in top_hypos:
                name, sport, mkt, thesis, events, signals, avg_edge = h
                edge_str = f"{avg_edge*100:+.2f}%" if avg_edge else "N/A"
                lines.append(f"  {name} ({sport}/{mkt}): {events} events, {signals} signals, edge {edge_str}")

        if rejected:
            lines.append("Recently disproven:")
            for r in rejected:
                lines.append(f"  {r[0]}: {r[1][:80]}")

        lines.append("</memory>")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"Research state build failed: {e}")
        return ""


async def build_learnings(db: aiosqlite.Connection) -> str:
    """Discoveries Claude has made and stored back to Hermes.

    EPISTEMICS (P4):
      1. Effective confidence DECAYS with age (memory_epistemics.decay_
         confidence) — a learning that is not re-observed loses standing.
         No ratchet survives into the prompt.
      2. Trimming is DISCONFIRMING-BIASED (consistent with
         tools/loop_quality.compact_state): when the section exceeds its
         budget, contradicting items outrank supporting ones for
         retention. The one disconfirming source is the most expensive
         thing to lose; dropping it silently corrupts the conclusion.
      3. Every emitted line carries its provenance class and confidence
         ceiling so a reinjected INFERRED learning is never mistaken for
         primary evidence.
    """
    try:
        rows = await db.execute_fetchall(
            "SELECT key, value, confidence, occurrences, learned_at, source "
            "FROM hermes_learnings ORDER BY learned_at DESC LIMIT 60"
        )
        if not rows:
            return ""

        from tools.memory_epistemics import (
            annotate_for_reinjection, decay_confidence, trim_learnings_for_context,
        )
        now = datetime.now(timezone.utc)
        items = []
        for r in rows:
            key, value, conf, occ, learned_at, source = r
            eff = decay_confidence(conf, learned_at, now)
            item = annotate_for_reinjection({
                "id": key,
                "value": value,
                "confidence": conf,
                "effective_confidence": eff,
                "occurrences": occ,
                "learned_at": learned_at,
                "source": source,
                # Memory rows do not carry stance today; only explicitly
                # marked disconfirmations count (conservative — see
                # trim_learnings_for_context).
                "stance": "contradicting" if str(key).startswith("disconfirm") or "contradict" in str(key).lower() else "supporting",
                # Provenance tier for retention ranking: human/audit best,
                # everything model-produced equal.
                "tier": 1 if source in ("human", "audit") else 3,
            })
            items.append(item)

        kept, dropped = trim_learnings_for_context(items, max_items=10)
        if dropped:
            logger.info(
                "learnings trimmed for context: kept %d, dropped %d "
                "(disconfirming-biased retention)",
                len(kept), len(dropped),
            )

        lines = ["<memory type=\"learnings\">"]
        lines.append("Discovered patterns (from your own analysis):")
        for it in kept:
            cls = it["source_class"]
            ceiling = it["confidence_ceiling"]
            lines.append(
                f"  [eff {it['effective_confidence']:.0%} conf, "
                f"provenance {cls} (ceiling {ceiling:.0%}), "
                f"{it['occurrences']}x seen] "
                f"{it['id']}: {str(it['value'])[:120]}"
            )
        lines.append(
            "NOTE: provenance class caps how strongly a learning may be "
            "treated — an INFERRED learning is a prior guess, not evidence."
        )
        lines.append("</memory>")
        return "\n".join(lines)
    except Exception:
        return ""


async def build_messages(db: aiosqlite.Connection) -> str:
    """Cross-session notifications — unread messages from other Claude sessions."""
    try:
        rows = await db.execute_fetchall(
            "SELECT timestamp, sender, message FROM hermes_messages "
            "WHERE read = 0 ORDER BY timestamp DESC LIMIT 5"
        )
        if not rows:
            return ""

        lines = ["<memory type=\"messages\">"]
        lines.append(f"UNREAD MESSAGES ({len(rows)}):")
        for r in rows:
            ts, sender, msg = r
            time_short = ts[11:16] if len(ts) > 16 else ts
            lines.append(f"  [{time_short}] {sender}: {msg[:150]}")
        lines.append("ACTION: Acknowledge these messages in your response.")
        lines.append("</memory>")
        return "\n".join(lines)
    except Exception:
        return ""


def build_code_changes() -> str:
    """Recent code changes from git — cross-session awareness."""
    try:
        repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        result = subprocess.run(
            ["git", "log", "--oneline", "-10", "--format=%h %s (%cr)"],
            cwd=repo_dir, capture_output=True, text=True, timeout=5,
        )
        commits = result.stdout.strip() if result.returncode == 0 else ""

        result2 = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, timeout=5,
        )
        uncommitted = result2.stdout.strip() if result2.returncode == 0 else ""

        if not commits and not uncommitted:
            return ""

        lines = ["<memory type=\"code_changes\">"]

        if commits:
            lines.append("Recent commits:")
            for c in commits.split("\n")[:10]:
                lines.append(f"  {c}")

        if uncommitted:
            lines.append("Uncommitted changes:")
            for u in uncommitted.split("\n")[-6:]:
                lines.append(f"  {u.strip()}")

        lines.append("</memory>")
        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"Code changes build failed: {e}")
        return ""
