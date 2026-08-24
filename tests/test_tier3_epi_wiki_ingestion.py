"""Tier 3 epistemics — wiki ingestion hardening.

New behavior (findings/instance4.md mechanism 3):
1. Sessions are ingested only when seal-verified (verify_seal on the stored
   full_session JSON) or unsealed-legacy (seal_hash NULL → treated as
   INFERRED-grade, capped at the INFERRED ceiling). A session whose seal is
   PRESENT but FAILS verification is rejected outright.
2. Article confidence = MIN of source confidences (never the mean), so a
   compiled article can never exceed its weakest source.
3. hermes learnings compile only when their CURRENT standing (read-time
   decay applied) clears the 0.5 gate, and an INFERRED-class learning must
   have been re-observed — a one-shot guess compiles nothing alone. Their
   contribution to an article cannot raise it above their own value
   (min rule covers this).
"""
import json

import aiosqlite
import pytest
import pytest_asyncio

from agp import AGPSession, Domain, Evidence, SessionStep, SessionSummary, SourceClass
from tools.knowledge_wiki import KnowledgeWiki


def _make_session(query="wiki ingestion probe", conclusion="a real conclusion") -> AGPSession:
    s = AGPSession(query)
    s.advance_to(SessionStep.ASSIGN_DOMAIN); s.domain = Domain.GENERAL
    s.advance_to(SessionStep.SOURCE_ENUMERATION); s.sources = ["x"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="observed fact", source_class=SourceClass.SECONDARY,
        confidence_score=0.70, domain=Domain.GENERAL, origin_agent="t"))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope=query, domain=Domain.GENERAL, conclusion=conclusion,
        confidence_score=0.70, evidence_count=1, contradiction_count=0)
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


@pytest_asyncio.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "test.db")) as conn:
        wiki = KnowledgeWiki(str(tmp_path / "test.db"))
        await wiki.initialize(conn)
        # minimal sessions table mirroring memory.py schema columns used here
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, query TEXT NOT NULL,
                domain TEXT NOT NULL, scope TEXT NOT NULL, conclusion TEXT,
                confidence_score REAL, confidence_tier TEXT,
                evidence_count INTEGER DEFAULT 0, contradiction_count INTEGER DEFAULT 0,
                manager_objections TEXT DEFAULT '[]', full_session TEXT NOT NULL,
                seal_hash TEXT, started_at TEXT NOT NULL, sealed_at TEXT)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS catalogue (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                origin_agent TEXT, content TEXT, source_class TEXT,
                confidence_score REAL, confidence_tier TEXT, domain TEXT,
                source_name TEXT, created_at TEXT)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS hermes_learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE,
                value TEXT, learned_at TEXT, confidence REAL, occurrences INTEGER,
                source TEXT, source_class TEXT)
        """)
        await conn.commit()
        yield conn, wiki


def _insert_session_sql():
    return ("INSERT INTO sessions (session_id, query, domain, scope, conclusion, "
            "confidence_score, confidence_tier, evidence_count, contradiction_count, "
            "manager_objections, full_session, seal_hash, started_at, sealed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


class TestSealVerifiedIngestion:
    @pytest.mark.asyncio
    async def test_valid_seal_is_ingested(self, db):
        conn, wiki = db
        s = _make_session()
        s.seal()
        d = s.to_dict()
        await conn.execute(_insert_session_sql(), (
            d["session_id"], d["query"], "GENERAL", d["scope"],
            "a real conclusion", 0.70, "PROBABLE", 1, 0, "[]",
            json.dumps(d), s.seal_hash, d["started_at"], d["sealed_at"]))
        await conn.commit()
        sources = await wiki._get_uncompiled_sources(conn)
        assert [x["id"] for x in sources if x["type"] == "session"] == [d["session_id"]]

    @pytest.mark.asyncio
    async def test_broken_seal_is_rejected(self, db):
        """A tampered session must never become prompt-context prior."""
        conn, wiki = db
        s = _make_session()
        s.seal()
        d = s.to_dict()
        d["conclusion"] = "TAMPERED CONCLUSION"          # swap bytes post-seal
        await conn.execute(_insert_session_sql(), (
            d["session_id"], d["query"], "GENERAL", d["scope"],
            "TAMPERED CONCLUSION", 0.70, "PROBABLE", 1, 0, "[]",
            json.dumps(d), s.seal_hash, d["started_at"], d["sealed_at"]))
        await conn.commit()
        sources = await wiki._get_uncompiled_sources(conn)
        assert [x for x in sources if x["type"] == "session"] == []

    @pytest.mark.asyncio
    async def test_legacy_unsealed_row_enters_capped(self, db):
        """Pre-sealing legacy rows (seal_hash NULL): content admitted for
        compilation but marked provenance INFERRED and confidence capped at
        the INFERRED ceiling — they may not manufacture CORROBORATED priors."""
        conn, wiki = db
        from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
        cap = MAX_CONFIDENCE_BY_SOURCE["INFERRED"]
        now = "2026-08-22T00:00:00+00:00"
        await conn.execute(_insert_session_sql(), (
            "legacy1", "q", "GENERAL", "q", "old conclusion",
            0.74, "PROBABLE", 1, 0, "[]",
            json.dumps({"query": "legacy probe"}), None, now, now))
        await conn.commit()
        sources = await wiki._get_uncompiled_sources(conn)
        sess = [x for x in sources if x["type"] == "session"]
        assert len(sess) == 1
        assert sess[0]["confidence"] <= cap
        assert sess[0]["provenance_class"] == "INFERRED"


class TestMinOfCeilings:
    def test_article_confidence_is_min_not_mean(self):
        """Two SECONDARY items at ceiling: mean manufactures CORROBORATED;
        min keeps the article honest — it is only as strong as its weakest
        source."""
        confs = [0.75, 0.75]
        assert sum(confs) / len(confs) == 0.75           # old behavior
        assert min(confs) == 0.75                        # same here…
        # …but diverges when sources differ:
        mixed = [0.75, 0.40]
        assert sum(mixed) / len(mixed) > max(mixed) - 0.30
        assert min(mixed) == 0.40

    def test_article_never_exceeds_weakest_source(self):
        from tools.knowledge_wiki import _article_confidence
        assert _article_confidence([{"confidence": 0.75}, {"confidence": 0.55}]) == 0.55
        assert _article_confidence([{"confidence": 0.9}]) == 0.9

    def test_weighted_merge_respects_weakest_floor(self):
        """The update merge keeps historical weight, but new merged
        confidence can no longer exceed min(existing, new-source-min)."""
        from tools.knowledge_wiki import _merged_article_confidence
        merged = _merged_article_confidence(existing_confidence=0.78,
                                            compile_count=10,
                                            new_sources=[{"confidence": 0.35}])
        assert merged <= 0.78 + 1e-9
        assert merged == pytest.approx(0.35, abs=1e-6)
