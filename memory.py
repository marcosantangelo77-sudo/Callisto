"""
Compartmentalized memory system for Callisto.

Single SQLite DB with WAL mode. Domain isolation enforced via CHECK constraints.
Views provide per-domain world access. Cross-domain reads are permanently logged.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from agp import (
    ConfidenceTier,
    Domain,
    Evidence,
    AGPSession,
)

load_dotenv()

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalogue (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    origin_agent TEXT NOT NULL,
    content TEXT NOT NULL,
    source_class TEXT NOT NULL CHECK(source_class IN ('PRIMARY', 'SECONDARY', 'SIGNAL', 'INFERRED')),
    confidence_score REAL NOT NULL CHECK(confidence_score >= 0.30),
    confidence_tier TEXT NOT NULL CHECK(confidence_tier IN ('VERIFIED', 'CORROBORATED', 'PROBABLE', 'SPECULATIVE')),
    domain TEXT NOT NULL CHECK(domain IN ('FINANCIAL', 'TECHNICAL', 'SIGNAL', 'SYNTHESIS', 'GENERAL')),
    source_name TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    promotion_history TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    domain TEXT NOT NULL,
    scope TEXT NOT NULL,
    conclusion TEXT,
    confidence_score REAL,
    confidence_tier TEXT,
    evidence_count INTEGER DEFAULT 0,
    contradiction_count INTEGER DEFAULT 0,
    manager_objections TEXT DEFAULT '[]',
    full_session TEXT NOT NULL,
    seal_hash TEXT,
    started_at TEXT NOT NULL,
    sealed_at TEXT
);

CREATE TABLE IF NOT EXISTS cross_domain_access_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    requesting_agent TEXT NOT NULL,
    requesting_domain TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    query_text TEXT NOT NULL,
    result_count INTEGER DEFAULT 0,
    accessed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIEW IF NOT EXISTS world_financial AS
    SELECT * FROM catalogue WHERE domain = 'FINANCIAL';

CREATE VIEW IF NOT EXISTS world_technical AS
    SELECT * FROM catalogue WHERE domain = 'TECHNICAL';

CREATE VIEW IF NOT EXISTS world_signal AS
    SELECT * FROM catalogue WHERE domain = 'SIGNAL';

CREATE VIEW IF NOT EXISTS world_synthesis AS
    SELECT * FROM catalogue WHERE domain = 'SYNTHESIS';

CREATE VIEW IF NOT EXISTS world_general AS
    SELECT * FROM catalogue WHERE domain = 'GENERAL';

CREATE INDEX IF NOT EXISTS idx_catalogue_domain ON catalogue(domain);
CREATE INDEX IF NOT EXISTS idx_catalogue_confidence ON catalogue(confidence_tier);
CREATE INDEX IF NOT EXISTS idx_catalogue_session ON catalogue(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_domain ON sessions(domain);
"""


class MemoryStore:
    """Compartmentalized memory with AGP-enforced domain isolation."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Create DB directory, open connection, initialize schema."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def store_evidence(self, session_id: str, evidence: Evidence) -> Optional[int]:
        """Store a piece of evidence. Returns entry_id or None if not storable."""
        tier = evidence.confidence_tier
        if not tier.is_storable:
            return None

        cursor = await self._db.execute(
            """INSERT INTO catalogue
               (session_id, origin_agent, content, source_class, confidence_score,
                confidence_tier, domain, source_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                evidence.origin_agent,
                evidence.content,
                evidence.source_class.value,
                evidence.confidence_score,
                tier.value,
                evidence.domain.value,
                evidence.source_name,
                evidence.timestamp,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def store_session(self, session: AGPSession) -> None:
        """Store a sealed AGP session."""
        session_dict = session.to_dict()
        summary = session.summary

        await self._db.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, query, domain, scope, conclusion, confidence_score,
                confidence_tier, evidence_count, contradiction_count,
                manager_objections, full_session, seal_hash, started_at, sealed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.session_id,
                session.query,
                session.domain.value if session.domain else "GENERAL",
                session.scope,
                summary.conclusion if summary else None,
                summary.confidence_score if summary else None,
                summary.confidence_tier.value if summary else None,
                len(session.evidence),
                len(session.contradictions),
                json.dumps(session.manager_objections),
                json.dumps(session_dict, ensure_ascii=False),
                session.seal_hash,
                session.started_at,
                session.sealed_at,
            ),
        )
        await self._db.commit()

    async def query_world(
        self,
        domain: Domain,
        keyword: Optional[str] = None,
        min_confidence: Optional[float] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query a domain world. Returns evidence entries."""
        view = f"world_{domain.value.lower()}"
        conditions = []
        params = []

        if keyword:
            conditions.append("content LIKE ?")
            params.append(f"%{keyword}%")
        if min_confidence is not None:
            conditions.append("confidence_score >= ?")
            params.append(min_confidence)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = await self._db.execute_fetchall(
            f"SELECT * FROM {view}{where} ORDER BY created_at DESC LIMIT ?",
            params,
        )
        if not rows:
            return []

        columns = [
            "entry_id", "session_id", "origin_agent", "content", "source_class",
            "confidence_score", "confidence_tier", "domain", "source_name",
            "created_at", "promotion_history",
        ]
        return [dict(zip(columns, row)) for row in rows]

    async def cross_domain_query(
        self,
        requesting_agent: str,
        requesting_domain: Domain,
        target_domain: Domain,
        keyword: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Query across domain boundaries. Automatically logged."""
        results = await self.query_world(target_domain, keyword=keyword, limit=limit)

        # Permanent audit trail
        await self._db.execute(
            """INSERT INTO cross_domain_access_log
               (requesting_agent, requesting_domain, target_domain,
                query_text, result_count, accessed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                requesting_agent,
                requesting_domain.value,
                target_domain.value,
                keyword or "*",
                len(results),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._db.commit()
        return results

    async def get_session(self, session_id: str) -> Optional[dict]:
        """Retrieve a sealed session by ID."""
        row = await self._db.execute_fetchall(
            "SELECT full_session FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        if row:
            return json.loads(row[0][0])
        return None
