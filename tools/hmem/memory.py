"""HermesMemory — Callisto's persistent, bidirectional, context-aware memory.

Extracted from tools/hermes_memory.py during the tools.hmem split.
``tools/hermes_memory.py`` remains the public import surface (facade).
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.hmem.identity import build_identity
from tools.hmem.sanitize import sanitize_learning_key, sanitize_learning_value
from tools.hmem.sections import (
    build_active_state,
    build_bet_history,
    build_code_changes,
    build_edge_history,
    build_learned_patterns,
    build_learnings,
    build_messages,
    build_research_state,
)

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

    # SECURITY (audit C-4) sanitizers live in tools.hmem.sanitize; kept as
    # staticmethods on the class for backward compatibility with callers that
    # poke at HermesMemory._sanitize_* directly in tests.
    _sanitize_learning_value = staticmethod(sanitize_learning_value)
    _sanitize_learning_key = staticmethod(sanitize_learning_key)

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
                    "identity": build_identity(),
                    "bets": await build_bet_history(db),
                    "edges": await build_edge_history(db),
                    "patterns": await build_learned_patterns(db),
                    "active": await build_active_state(db),
                    "research": await build_research_state(db),
                    "learnings": await build_learnings(db),
                    "messages": await build_messages(db),
                    "code": build_code_changes(),
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
            return build_identity() + "\n\n⚠️ HERMES CONTEXT DEGRADED: Full memory unavailable."

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

    async def record_learning(
        self,
        key: str,
        value: str,
        confidence: float = 0.5,
        source: str = "claude",
        source_class=None,
        seal_session: dict | None = None,
        seal_hash: str | None = None,
    ) -> None:
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

        EPISTEMICS (P4, kills the trust escalator — findings/instance4.md P3):
        the previous upsert used ``confidence=MAX(confidence, excluded.confidence)``
        (quoted here only as history — no such upsert remains),
        a one-way ratchet that let one optimistic self-report contaminate a key
        forever and let the wiki admit unverified guesses at >= 0.5. Semantics now:

          - confidence REPLACES on upsert (no ratchet) and is clamped to the
            ceiling of the learning's PROVENANCE class (see memory_epistemics);
          - a claimed class above INFERRED requires a verifying seal carried in
            ``seal_session``/``seal_hash``; unsealed or failed-seal claims are
            capped to INFERRED (fail closed);
          - human/audit sources may exceed their class ceiling (operator channels).

        Stored-data semantics change: existing rows were written under the old
        MAX-ratchet upsert, so migration 015_hermes_confidence_decay resets
        contaminated rows (dry-run-first, reversible; NOT auto-run here).
        """
        try:
            key = sanitize_learning_key(key)
            value = sanitize_learning_value(value)
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.5
            if source not in ("claude", "callisto", "hermes", "agent", "human", "self_repair", "audit"):
                source = "claude"
            from tools.memory_epistemics import admit_learning
            admission = admit_learning(
                key=key,
                confidence=confidence,
                source=source,
                # Trusted operator channels (human/audit) bypass ceilings in
                # admit_learning; everyone else defaults to INFERRED until they
                # carry provenance.
                source_class=source_class,
                seal_session=seal_session,
                seal_hash=seal_hash,
            )
            confidence = admission.stored_confidence
            provenance_class = admission.source_class
            # WriteCoordinator path (single-writer pattern). Skips opening yet
            # another connection just to write one row.
            try:
                from tools.db_writer import get_writer_if_running
                coord = get_writer_if_running(self.db_path)
            except Exception:
                coord = None
            if coord is not None:
                # Tables are guaranteed by ensure_schema at startup; the legacy
                # _ensure_tables call here is a belt-and-braces idempotent CREATE
                # IF NOT EXISTS that we skip on the coordinator path.
                if not self._db_initialized:
                    async with aiosqlite.connect(self.db_path) as db:
                        await self._ensure_tables(db)
                await coord.execute(
                    "INSERT INTO hermes_learnings (key, value, learned_at, confidence, source) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, occurrences=occurrences+1, "
                    "confidence=excluded.confidence, "
                    "learned_at=excluded.learned_at, source=excluded.source",
                    (key, value, datetime.now(timezone.utc).isoformat(), confidence, source),
                )
                self._cache.clear()
                self._cache_time.clear()
                logger.info(
                    f"Hermes learning recorded: {key} (confidence={confidence:.2f}, "
                    f"class={provenance_class}; {admission.reason})"
                )
                return
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                await self._ensure_tables(db)
                await db.execute(
                    "INSERT INTO hermes_learnings (key, value, learned_at, confidence, source) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, occurrences=occurrences+1, "
                    "confidence=excluded.confidence, "
                    "learned_at=excluded.learned_at, source=excluded.source",
                    (key, value, datetime.now(timezone.utc).isoformat(), confidence, source),
                )
                await db.commit()
                # Invalidate cache so next read picks up the new learning
                self._cache.clear()
                self._cache_time.clear()
                logger.info(
                    f"Hermes learning recorded: {key} (confidence={confidence:.2f}, "
                    f"class={provenance_class}; {admission.reason})"
                )
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
                from tools.memory_epistemics import annotate_for_reinjection
                return [
                    annotate_for_reinjection({
                        "key": r[0],
                        "value": r[1],
                        "confidence": r[2],
                        "occurrences": r[3],
                        "source": r[4],
                        "learned_at": r[5],
                    })
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


# Singleton
_instance: Optional[HermesMemory] = None


def get_hermes_memory() -> HermesMemory:
    """Get the singleton HermesMemory instance."""
    global _instance
    if _instance is None:
        _instance = HermesMemory()
    return _instance
