"""
Hermes — Callisto's nervous system. Persistent memory + bidirectional bridge.

Hermes is NOT just a context builder. It is the continuous memory layer that:
1. READS state → builds context for every Claude call (identity, bets, edges, research, code)
2. WRITES back ← Claude stores discoveries, learnings, and insights after each call
3. PRIORITIZES sections based on caller intent (hypothesis gen vs edge analysis vs deep work)
4. NOTIFIES across sessions via a message queue (cross-session awareness)

Every Claude CLI subprocess gets Hermes context automatically via the bridge
in claude_code.py. No call is stateless. No session is blind.

Memory sections:
1. IDENTITY       — who Callisto is, rules, capabilities
2. BETS           — bankroll, open bets, P/L, CLV track record
3. EDGES          — recent +EV opportunities, analysis sessions
4. PATTERNS       — learned market/book patterns from operation
5. ACTIVE STATE   — open bets, current monitoring
6. RESEARCH       — hypothesis counts, top tested, recently disproven
7. CODE CHANGES   — git commits, uncommitted modifications (cross-session awareness)
8. LEARNINGS      — discoveries Claude has made (bidirectional memory)
9. MESSAGES       — cross-session notification queue
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.hermes")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
MESSAGES_FILE = os.path.join(os.path.dirname(DB_PATH), "hermes_messages.json")

# Context caller types — determines which sections get priority
CALLER_HYPOTHESIS_GEN = "hypothesis_gen"
CALLER_DEEP_WORK = "deep_work"
CALLER_EDGE_ANALYSIS = "edge_analysis"
CALLER_TELEGRAM = "telegram"
CALLER_DEFAULT = "default"


class HermesMemory:
    """Callisto's nervous system — persistent, bidirectional, context-aware memory."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._cache: dict[str, str] = {}  # caller_type -> cached context (max 20 entries)
        self._cache_time: dict[str, float] = {}
        self._cache_ttl: float = 90  # Refresh every 90 seconds
        self._cache_max_entries: int = 20  # Hard cap to prevent memory leak
        self._db_initialized = False

    async def _ensure_tables(self, db: aiosqlite.Connection) -> None:
        """Create Hermes tables if they don't exist."""
        if self._db_initialized:
            return
        # SECURITY (audit C-6): split executescript into individual execute() so we
        # don't hold an EXCLUSIVE lock for the duration of multi-statement DDL.
        for stmt in (
            """CREATE TABLE IF NOT EXISTS hermes_learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL,
                learned_at TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                occurrences INTEGER DEFAULT 1,
                source TEXT DEFAULT 'claude'
            )""",
            """CREATE TABLE IF NOT EXISTS hermes_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                sender TEXT NOT NULL,
                message TEXT NOT NULL,
                read INTEGER DEFAULT 0
            )""",
        ):
            await db.execute(stmt)
        await db.commit()
        self._db_initialized = True

    # SECURITY (audit C-4): sanitize values before storing so prompt-injection
    # markers can't survive the round-trip through hermes_learnings → context
    # → next Claude prompt. We neutralize tag-like metacharacters, code-fence
    # markers, and common system-prompt sentinels, and cap the length.
    @staticmethod
    def _sanitize_learning_value(value: str) -> str:
        if not isinstance(value, str):
            value = str(value)
        # Cap before substitution so attacker-supplied massive payload doesn't
        # consume CPU re-stringifying. 4KB is plenty for a learning summary.
        if len(value) > 4096:
            value = value[:4096] + " …[truncated]"
        # Neutralize HTML/XML-ish tags and code fences. We escape rather than
        # strip so the original signal stays human-readable in audits.
        value = (
            value.replace("\u200b", "")  # zero-width space sometimes used to bypass filters
                 .replace("<", "‹")
                 .replace(">", "›")
                 .replace("```", "ʼʼʼ")
                 .replace("\x00", "")
        )
        # Strip common LLM jailbreak sentinels by escaping the leading bracket.
        for sentinel in (
            "[INST]", "[/INST]",
            "[SYSTEM]", "[/SYSTEM]",
            "{{system}}", "{{/system}}",
            "<|im_start|>", "<|im_end|>",
        ):
            value = value.replace(sentinel, sentinel.replace("[", "(").replace("]", ")")
                                  .replace("<", "‹").replace(">", "›")
                                  .replace("{", "(").replace("}", ")"))
        return value

    @staticmethod
    def _sanitize_learning_key(key: str) -> str:
        """Keys must be short, ASCII-ish identifiers — they appear verbatim in prompts."""
        if not isinstance(key, str):
            key = str(key)
        key = key.strip()
        if not key:
            raise ValueError("learning key must be non-empty")
        if len(key) > 128:
            key = key[:128]
        # Permissive but no markup: letters, digits, underscore, dash, dot, colon, slash.
        import re as _re
        return _re.sub(r"[^A-Za-z0-9_\-\.:/]+", "_", key)

    # ──────────────────────────────────────────────────
    # READ: Build context for Claude calls
    # ──────────────────────────────────────────────────

    async def get_memory_context(
        self,
        caller: str = CALLER_DEFAULT,
        force_refresh: bool = False,
    ) -> str:
        """
        Build prioritized memory context for injection into Claude prompts.

        Args:
            caller: What's calling — determines section priority/ordering.
            force_refresh: Bypass cache.

        Returns:
            Compact text block with all relevant memory sections.
        """
        now = time.time()
        cache_key = caller
        if (not force_refresh
                and cache_key in self._cache
                and (now - self._cache_time.get(cache_key, 0)) < self._cache_ttl):
            return self._cache[cache_key]

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                await self._ensure_tables(db)

                # Build all sections
                all_sections = {
                    "identity": self._build_identity(),
                    "bets": await self._build_bet_history(db),
                    "edges": await self._build_edge_history(db),
                    "patterns": await self._build_learned_patterns(db),
                    "active": await self._build_active_state(db),
                    "research": await self._build_research_state(db),
                    "learnings": await self._build_learnings(db),
                    "messages": await self._build_messages(db),
                    "code": self._build_code_changes(),
                }

                # Priority ordering based on caller
                order = self._get_section_order(caller)

                sections = []
                for key in order:
                    section = all_sections.get(key, "")
                    if section:
                        sections.append(section)

                result = "\n\n".join(sections)
                # Enforce max cache size to prevent memory leak
                if len(self._cache) >= self._cache_max_entries:
                    # Evict oldest entries
                    oldest = sorted(self._cache_time, key=self._cache_time.get)
                    for old_key in oldest[:len(oldest) // 2]:
                        self._cache.pop(old_key, None)
                        self._cache_time.pop(old_key, None)
                self._cache[cache_key] = result
                self._cache_time[cache_key] = now
                return result

        except Exception as e:
            logger.error(
                f"Hermes context build DEGRADED — returning identity-only context. "
                f"Claude will operate without bets/edges/research/learnings. Error: {e}"
            )
            return self._build_identity() + "\n\n⚠️ HERMES CONTEXT DEGRADED: Full memory unavailable."

    def _get_section_order(self, caller: str) -> list[str]:
        """Return section keys in priority order based on caller type."""
        if caller == CALLER_HYPOTHESIS_GEN:
            # Research state and learnings first — what's been tried, what works
            return ["identity", "research", "learnings", "patterns", "edges", "code", "messages", "bets", "active"]
        elif caller == CALLER_DEEP_WORK:
            # Everything matters — deep work is the most comprehensive phase
            return ["identity", "research", "learnings", "patterns", "edges", "bets", "active", "code", "messages"]
        elif caller == CALLER_EDGE_ANALYSIS:
            # Edges and patterns first — what markets/books produce value
            return ["identity", "edges", "patterns", "active", "bets", "research", "learnings", "code", "messages"]
        elif caller == CALLER_TELEGRAM:
            # Active state and bets first — Marco is checking in
            return ["identity", "active", "bets", "edges", "research", "messages", "learnings", "patterns", "code"]
        else:
            return ["identity", "bets", "edges", "patterns", "active", "research", "learnings", "messages", "code"]

    # ──────────────────────────────────────────────────
    # WRITE: Claude stores discoveries back to Hermes
    # ──────────────────────────────────────────────────

    async def record_learning(self, key: str, value: str, confidence: float = 0.5, source: str = "claude") -> None:
        """
        Store a discovery/pattern for future calls.

        Called by Claude deep work and backtest interpretation phases.
        Examples:
          - "dk_h2h_lag_pinnacle" → "DraftKings h2h lines lag Pinnacle by ~12 min on NBA"
          - "cold_venue_under_edge" → "Unders at northern parks in April show +1.5% avg edge"
          - "backtest_13_same_game" → "13 MLB hypotheses all flagged same game — need better filtering"

        SECURITY (audit C-4): value is sanitized to neutralize prompt-injection
        sentinels because every learning is later re-injected verbatim into
        Claude's prompt context.
        """
        try:
            key = self._sanitize_learning_key(key)
            value = self._sanitize_learning_value(value)
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.5
            confidence = max(0.0, min(1.0, confidence))
            if source not in ("claude", "callisto", "hermes", "agent", "human", "self_repair", "audit"):
                source = "claude"
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                await self._ensure_tables(db)
                await db.execute(
                    "INSERT INTO hermes_learnings (key, value, learned_at, confidence, source) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, occurrences=occurrences+1, "
                    "confidence=MAX(confidence, excluded.confidence), "
                    "learned_at=excluded.learned_at, source=excluded.source",
                    (key, value, datetime.now(timezone.utc).isoformat(), confidence, source),
                )
                await db.commit()
                # Invalidate cache so next read picks up the new learning
                self._cache.clear()
                self._cache_time.clear()
                logger.info(f"Hermes learning recorded: {key} (confidence={confidence:.2f})")
        except Exception as e:
            logger.error(f"Failed to record learning: {e}")

    async def record_learnings_batch(self, learnings: list[dict]) -> int:
        """
        Store multiple learnings at once. Each dict needs 'key' and 'value',
        optionally 'confidence' and 'source'.

        Returns count of successfully stored learnings.
        """
        stored = 0
        for l in learnings:
            try:
                await self.record_learning(
                    key=l["key"],
                    value=l["value"],
                    confidence=l.get("confidence", 0.5),
                    source=l.get("source", "claude"),
                )
                stored += 1
            except Exception as e:
                logger.warning(f"Failed to record learning '{l.get('key')}': {e}")
        return stored

    async def get_actionable_learnings(self, limit: int = 10, min_confidence: float = 0.5) -> list[dict]:
        """
        Get recent, high-confidence learnings for injection into Claude prompts.

        Returns learnings that represent actionable intelligence:
        - Pipeline issues that need attention
        - Patterns discovered from data
        - Bugs or misconfigurations detected

        These are injected into deep_work prompts so Claude builds on
        prior discoveries instead of rediscovering the same issues.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                await self._ensure_tables(db)
                cursor = await db.execute(
                    "SELECT key, value, confidence, occurrences, source, learned_at "
                    "FROM hermes_learnings "
                    "WHERE confidence >= ? "
                    "ORDER BY learned_at DESC LIMIT ?",
                    (min_confidence, limit),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "key": r[0],
                        "value": r[1],
                        "confidence": r[2],
                        "occurrences": r[3],
                        "source": r[4],
                        "learned_at": r[5],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.debug(f"Failed to get actionable learnings: {e}")
            return []

    # ──────────────────────────────────────────────────
    # NOTIFY: Cross-session message queue
    # ──────────────────────────────────────────────────

    async def send_message(self, sender: str, message: str) -> None:
        """
        Post a message for other sessions to read.

        Examples:
          - ("termius_session", "Rewrote backtest engine — all hypothesis thresholds lowered to 1.5%")
          - ("research_loop", "Found 3 hypotheses with positive edge >2% on MLB unders")
          - ("deep_work", "Pipeline integrity issue: 13 hypotheses tested same game")
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                await self._ensure_tables(db)
                await db.execute(
                    "INSERT INTO hermes_messages (timestamp, sender, message) VALUES (?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(), sender, message),
                )
                await db.commit()
                self._cache.clear()
                self._cache_time.clear()
                logger.info(f"Hermes message from {sender}: {message[:80]}")
        except Exception as e:
            logger.error(f"Failed to send Hermes message: {e}")

    async def get_unread_messages(self) -> list[dict]:
        """Get all unread messages and mark them as read."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                await self._ensure_tables(db)
                rows = await db.execute_fetchall(
                    "SELECT id, timestamp, sender, message FROM hermes_messages "
                    "WHERE read = 0 ORDER BY timestamp"
                )
                if rows:
                    ids = [r[0] for r in rows]
                    placeholders = ",".join("?" * len(ids))
                    await db.execute(
                        f"UPDATE hermes_messages SET read = 1 WHERE id IN ({placeholders})",
                        ids,
                    )
                    await db.commit()
                return [
                    {"id": r[0], "timestamp": r[1], "sender": r[2], "message": r[3]}
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"Failed to get Hermes messages: {e}")
            return []

    # ──────────────────────────────────────────────────
    # Section builders
    # ──────────────────────────────────────────────────

    def _build_identity(self) -> str:
        """Core identity — who Callisto is and what it does."""
        return (
            "<memory type=\"identity\">\n"
            "You are Callisto \u2014 an autonomous general-purpose research agent.\n"
            "Owner: Marco Santangelo. Primary domain: quantitative edge detection.\n"
            "Books: DraftKings (primary), Fanatics (secondary).\n"
            "Core method: devig sharp books (Pinnacle) to find true probability,\n"
            "compare to soft books (DK/FanDuel/BetMGM) for mispricing.\n"
            "You are Claude Opus 4.6 \u2014 the PRIMARY reasoning engine.\n"
            "Local models (Sentinel) handle lightweight tasks only.\n"
            "DISPOSITION:\n"
            "- You are a skeptic first. Your default: any signal is noise until proven.\n"
            "- You challenge your own output before returning it.\n"
            "- You flag broken pipelines before generating new hypotheses.\n"
            "- You are adversarial toward sycophancy \u2014 telling the system what it wants\n"
            "  to hear is the fastest way to waste cycles on garbage.\n"
            "- When data quality is insufficient to test a hypothesis, say so plainly\n"
            "  rather than generating results that look productive but mean nothing.\n"
            "RULES:\n"
            "- Never recommend bets without quantitative evidence\n"
            "- Scrutinize backtests: how many books contributed? Are event counts suspiciously identical?\n"
            "- Track record matters \u2014 every bet gets CLV-measured\n"
            "- Think outside the box \u2014 absurd hypotheses can have the biggest edges\n"
            "- Callisto is NOT just sports \u2014 stocks, crypto, any quantifiable edge\n"
            "- When you discover something, WRITE IT BACK via record_learning()\n"
            "- Check messages section for cross-session notifications\n"
            "</memory>"
        )

    async def _build_bet_history(self, db: aiosqlite.Connection) -> str:
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

    async def _build_edge_history(self, db: aiosqlite.Connection) -> str:
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

    async def _build_learned_patterns(self, db: aiosqlite.Connection) -> str:
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

    async def _build_active_state(self, db: aiosqlite.Connection) -> str:
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

    async def _build_research_state(self, db: aiosqlite.Connection) -> str:
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

    async def _build_learnings(self, db: aiosqlite.Connection) -> str:
        """Discoveries Claude has made and stored back to Hermes."""
        try:
            rows = await db.execute_fetchall(
                "SELECT key, value, confidence, occurrences, learned_at "
                "FROM hermes_learnings ORDER BY confidence DESC, occurrences DESC LIMIT 10"
            )
            if not rows:
                return ""

            lines = ["<memory type=\"learnings\">"]
            lines.append("Discovered patterns (from your own analysis):")
            for r in rows:
                key, value, conf, occ, when = r
                lines.append(f"  [{conf:.0%} conf, {occ}x seen] {key}: {value[:120]}")
            lines.append("</memory>")
            return "\n".join(lines)
        except Exception:
            return ""

    async def _build_messages(self, db: aiosqlite.Connection) -> str:
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

    def _build_code_changes(self) -> str:
        """Recent code changes from git — cross-session awareness."""
        try:
            repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


# Singleton
_instance: Optional[HermesMemory] = None


def get_hermes_memory() -> HermesMemory:
    """Get the singleton HermesMemory instance."""
    global _instance
    if _instance is None:
        _instance = HermesMemory()
    return _instance


# Backward compat
def get_cache_manager():
    """Get the tiered CacheManager (hot/warm/cold). Preferred over HermesMemory."""
    from tools.cache_manager import get_cache_manager as _get_cm
    return _get_cm()
