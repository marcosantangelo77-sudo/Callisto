"""
Hermes persistent memory — gives local agents memory across sessions.

Without this, every AGP session starts from scratch. The Architect doesn't know
what edges it found yesterday, which bets won, or what patterns it's seen.

This module builds a compressed memory context that gets injected into the
Architect's system prompt on every inference call. The agents carry their
history at all times, no Claude Code needed.

Memory is organized into:
1. EDGE HISTORY    — recent edges found, which were real vs noise
2. BET OUTCOMES    — win/loss/CLV track record, bankroll state
3. LEARNED PATTERNS — which markets/books/teams produce consistent edges
4. ACTIVE STATE    — current open bets, today's games, active alerts
5. SYSTEM IDENTITY — who Callisto is, what it's trying to do
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.hermes_memory")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Memory budget — must fit in Architect's 8192 context alongside the query + tools
MAX_MEMORY_TOKENS_APPROX = 2000  # ~2000 tokens ≈ ~1500 words


class HermesMemory:
    """Builds and maintains the persistent memory context for local agents."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._cache: Optional[str] = None
        self._cache_time: float = 0
        self._cache_ttl: float = 120  # Refresh every 2 minutes

    async def get_memory_context(self, force_refresh: bool = False) -> str:
        """
        Build the full memory context string for injection into agent prompts.

        Returns a compact text block that fits in ~2000 tokens.
        Cached for 2 minutes to avoid hitting DB on every inference call.
        """
        now = time.time()
        if self._cache and not force_refresh and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        try:
            async with aiosqlite.connect(self.db_path) as db:
                sections = []

                # 1. System identity
                sections.append(self._build_identity())

                # 2. Bet outcomes & bankroll
                bet_section = await self._build_bet_history(db)
                if bet_section:
                    sections.append(bet_section)

                # 3. Recent edge history
                edge_section = await self._build_edge_history(db)
                if edge_section:
                    sections.append(edge_section)

                # 4. Learned patterns
                pattern_section = await self._build_learned_patterns(db)
                if pattern_section:
                    sections.append(pattern_section)

                # 5. Active state
                active_section = await self._build_active_state(db)
                if active_section:
                    sections.append(active_section)

                self._cache = "\n\n".join(sections)
                self._cache_time = now
                return self._cache

        except Exception as e:
            logger.error(f"Memory context build failed: {e}")
            return self._build_identity()  # Fallback to identity only

    def _build_identity(self) -> str:
        """Core identity — who Callisto is and what it does."""
        return (
            "<memory type=\"identity\">\n"
            "You are Callisto — an autonomous sports betting edge detection system.\n"
            "Owner: Marco Santangelo. Books: DraftKings (primary), Fanatics (secondary).\n"
            "Your job: find +EV bets using quantitative edge detection, not gut picks.\n"
            "Core method: devig sharp books (Pinnacle/Circa) to find true probability,\n"
            "compare to soft books (DK/FanDuel/BetMGM) for mispricing.\n"
            "Edge = sharp consensus - soft book implied probability.\n"
            "All confidence scores are CAPPED by evidence quality (AGP protocol).\n"
            "You must NEVER recommend a bet without quantitative evidence.\n"
            "Track record matters — every bet gets CLV-measured against closing lines.\n"
            "</memory>"
        )

    async def _build_bet_history(self, db: aiosqlite.Connection) -> str:
        """Bet outcomes and bankroll state."""
        # Current bankroll
        bal_row = await db.execute_fetchall(
            "SELECT balance FROM bankroll ORDER BY timestamp DESC LIMIT 1"
        )
        bankroll = bal_row[0][0] if bal_row else "unknown"

        # Bet summary
        total_row = await db.execute_fetchall("SELECT COUNT(*) FROM bets")
        total_bets = total_row[0][0] if total_row else 0

        won_row = await db.execute_fetchall("SELECT COUNT(*) FROM bets WHERE result='won'")
        lost_row = await db.execute_fetchall("SELECT COUNT(*) FROM bets WHERE result='lost'")
        pending_row = await db.execute_fetchall("SELECT COUNT(*) FROM bets WHERE result='pending'")
        won = won_row[0][0] if won_row else 0
        lost = lost_row[0][0] if lost_row else 0
        pending = pending_row[0][0] if pending_row else 0

        # P/L
        pl_row = await db.execute_fetchall(
            "SELECT COALESCE(SUM(CASE WHEN result='won' THEN payout - stake "
            "WHEN result='lost' THEN -stake ELSE 0 END), 0) FROM bets"
        )
        pl = pl_row[0][0] if pl_row else 0

        # CLV stats
        clv_row = await db.execute_fetchall(
            "SELECT AVG(clv_implied) FROM bets WHERE clv_implied IS NOT NULL"
        )
        avg_clv = clv_row[0][0] if clv_row and clv_row[0][0] is not None else None

        # Recent bets (last 5)
        recent = await db.execute_fetchall(
            "SELECT game_description, team, market, placement_odds, result, "
            "stake, payout, clv_implied FROM bets ORDER BY placed_at DESC LIMIT 5"
        )

        lines = [f"<memory type=\"bets\">"]
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
                    line += f" → +${payout - stake:.0f}"
                elif result == 'lost':
                    line += f" → -${stake:.0f}"
                if clv is not None:
                    line += f" (CLV: {clv:+.2%})"
                lines.append(line)

        lines.append("</memory>")
        return "\n".join(lines)

    async def _build_edge_history(self, db: aiosqlite.Connection) -> str:
        """Recent edge detection results from sessions."""
        # Recent autonomous sessions
        sessions = await db.execute_fetchall(
            "SELECT query, conclusion, confidence_score, confidence_tier, sealed_at "
            "FROM sessions WHERE query LIKE '%AUTONOMOUS%' OR query LIKE '%edge%' "
            "ORDER BY sealed_at DESC LIMIT 5"
        )

        # Recent EV opportunities
        ev_opps = await db.execute_fetchall(
            "SELECT sport, team, market, bookmaker, american_odds, edge, "
            "expected_value, kelly_fraction, detected_at "
            "FROM ev_opportunities ORDER BY detected_at DESC LIMIT 5"
        )

        if not sessions and not ev_opps:
            return ""

        lines = ["<memory type=\"edges\">"]

        if ev_opps:
            lines.append("Recent +EV opportunities detected:")
            for o in ev_opps:
                sport, team, market, book, odds, edge, ev, kelly, detected = o
                odds_str = f"+{odds}" if odds > 0 else str(odds)
                lines.append(
                    f"  {team} {market} @ {odds_str} ({book}) | "
                    f"edge={edge:.1%}, EV=${ev:.2f}, Kelly={kelly:.1%} [{detected[:10]}]"
                )

        if sessions:
            lines.append("Recent analysis sessions:")
            for s in sessions:
                query, conclusion, conf, tier, sealed = s
                # Truncate query for compactness
                q_short = query[:60].replace("\n", " ")
                c_short = (conclusion or "")[:80].replace("\n", " ")
                lines.append(f"  [{tier} {conf:.2f}] {q_short}... → {c_short}")

        lines.append("</memory>")
        return "\n".join(lines)

    async def _build_learned_patterns(self, db: aiosqlite.Connection) -> str:
        """Patterns derived from historical data."""
        # Which markets produce the most EV opportunities
        market_stats = await db.execute_fetchall(
            "SELECT market, COUNT(*) as cnt, AVG(edge) as avg_edge "
            "FROM ev_opportunities GROUP BY market ORDER BY cnt DESC LIMIT 5"
        )

        # Which books appear most in opportunities
        book_stats = await db.execute_fetchall(
            "SELECT bookmaker, COUNT(*) as cnt, AVG(edge) as avg_edge "
            "FROM ev_opportunities GROUP BY bookmaker ORDER BY cnt DESC LIMIT 5"
        )

        # Bet performance by market
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

    async def _build_active_state(self, db: aiosqlite.Connection) -> str:
        """Current open bets and active monitoring state."""
        # Open bets
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
                # First 60 chars of notes
                lines.append(f"    {notes[:60]}")

        lines.append("</memory>")
        return "\n".join(lines)

    async def record_learning(self, db: aiosqlite.Connection, key: str, value: str) -> None:
        """
        Store an explicit learning/pattern for future reference.

        These are things the system discovers through operation:
        - "DraftKings h2h lines lag Pinnacle by ~15 min on NBA"
        - "Player props under 0.5 points from round numbers have higher edge frequency"
        """
        await db.execute(
            "CREATE TABLE IF NOT EXISTS hermes_learnings ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  key TEXT NOT NULL UNIQUE,"
            "  value TEXT NOT NULL,"
            "  learned_at TEXT NOT NULL,"
            "  confidence REAL DEFAULT 0.5,"
            "  occurrences INTEGER DEFAULT 1"
            ")"
        )
        await db.execute(
            "INSERT INTO hermes_learnings (key, value, learned_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, occurrences=occurrences+1, "
            "learned_at=excluded.learned_at",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        logger.info(f"Learning recorded: {key}")


# Singleton instance
_instance: Optional[HermesMemory] = None


def get_hermes_memory() -> HermesMemory:
    """Get the singleton HermesMemory instance."""
    global _instance
    if _instance is None:
        _instance = HermesMemory()
    return _instance


# ──────────────────────────────────────────────────
# NEW: Tiered cache manager (hot/warm/cold)
# Use get_cache_manager() for new code.
# get_hermes_memory() still works for backward compat.
# ──────────────────────────────────────────────────
def get_cache_manager():
    """Get the tiered CacheManager (hot/warm/cold). Preferred over HermesMemory."""
    from tools.cache_manager import get_cache_manager as _get_cm
    return _get_cm()
