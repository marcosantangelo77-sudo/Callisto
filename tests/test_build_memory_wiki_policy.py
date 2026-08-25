"""BUILD improve run (build/memory-wiki) — ONE trust policy for everything
that feeds the wiki, applied at every ingestion site.

Family hunted: PATTERNS #2 (a fix lands in one copy while another keeps the
bug) with #3 (absence treated as success) as the mechanism inside each copy.
The P4/R7 memory trust policy landed in hermes record_learning and the wiki
compile path; these tests pin the sibling copies to the same policy:

  1. ingestion clamps every source's confidence to its provenance ceiling
     (legacy/pre-P4 rows included) and LABELS every source with a class;
  2. absent/NULL confidence enters at 0.0 — never manufactured up to 0.5;
  3. write_lesson_article merges confidence DOWNWARD (min) on update instead
     of replacing it with the caller's number;
  4. articles persist their weakest source provenance class;
  5. the /task wiki short-circuit refuses articles that earned nothing and
     propagates true confidence instead of ``or 0.5``.
"""
from __future__ import annotations

import json

import aiosqlite
import pytest
import pytest_asyncio

from agp import AGPSession, Domain, Evidence, SessionStep, SessionSummary, SourceClass
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
from tools.knowledge_wiki import KnowledgeWiki

INFERRED_CAP = MAX_CONFIDENCE_BY_SOURCE["INFERRED"]


def _make_session(query="policy probe", conclusion="a real conclusion") -> AGPSession:
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
                source TEXT)
        """)
        await conn.commit()
        yield conn, wiki


async def _insert_sealed_session(conn, conf):
    s = _make_session()
    s.seal()
    d = s.to_dict()
    await conn.execute(
        "INSERT INTO sessions (session_id, query, domain, scope, conclusion, "
        "confidence_score, confidence_tier, evidence_count, contradiction_count, "
        "manager_objections, full_session, seal_hash, started_at, sealed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (d["session_id"], d["query"], "GENERAL", d["scope"],
         "a real conclusion", conf, "PROBABLE", 1, 0, "[]",
         json.dumps(d), s.seal_hash, d["started_at"], d["sealed_at"]))
    await conn.commit()


def _session_sources(sources):
    return [x for x in sources if x["type"] == "session"]


class TestIngestionClamp:
    @pytest.mark.asyncio
    async def test_legacy_learning_row_is_capped_and_labeled(self, db):
        """A pre-P4 contaminated row (confidence 0.9 from before migration
        015) must enter compilation clamped to the INFERRED ceiling and
        labelled INFERRED — the wiki cannot be a laundering step."""
        conn, wiki = db
        await conn.execute(
            "INSERT INTO hermes_learnings (key, value, learned_at, confidence, "
            "occurrences, source) VALUES ('guess', 'unverified guess', ?, 0.9, 1, 'claude')",
            ("2026-08-20T00:00:00+00:00",))
        await conn.commit()
        sources = await wiki._get_uncompiled_sources(conn)
        learnings = [x for x in sources if x["type"] == "learning"]
        assert len(learnings) == 1
        assert learnings[0]["confidence"] <= INFERRED_CAP
        assert learnings[0]["provenance_class"] == "INFERRED"

    @pytest.mark.asyncio
    async def test_null_confidence_session_enters_at_zero_not_half(self, db):
        """Absence of a score must fail closed. ``conf or 0.5`` manufactured
        mid-confidence out of nothing."""
        conn, wiki = db
        await _insert_sealed_session(conn, None)
        sources = await wiki._get_uncompiled_sources(conn)
        sess = _session_sources(sources)
        assert len(sess) == 1
        assert sess[0]["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_zero_confidence_session_stays_zero(self, db):
        conn, wiki = db
        await _insert_sealed_session(conn, 0.0)
        sources = await wiki._get_uncompiled_sources(conn)
        sess = _session_sources(sources)
        assert len(sess) == 1
        assert sess[0]["confidence"] == 0.0

    @pytest.mark.asyncio
    async def test_catalogue_primary_evidence_keeps_class_and_value(self, db):
        """Explicitly-classed evidence keeps its declared class (and its
        number, within the ceiling) — gating must not flatten real tiers."""
        conn, wiki = db
        await conn.execute(
            "INSERT INTO catalogue (content, source_class, confidence_score, "
            "domain, created_at) VALUES ('measured fact', 'PRIMARY', 0.9, 'GENERAL', ?)",
            ("2026-08-20T00:00:00+00:00",))
        await conn.commit()
        sources = await wiki._get_uncompiled_sources(conn)
        ev = [x for x in sources if x["type"] == "evidence"]
        assert len(ev) == 1
        assert ev[0]["provenance_class"] == "PRIMARY"
        assert ev[0]["confidence"] == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_catalogue_unclassed_evidence_fails_closed(self, db):
        """catalogue was the last ungated ingestion path: a high number with
        no class used to flow straight in. NULL class ⇒ INFERRED ceiling."""
        conn, wiki = db
        await conn.execute(
            "INSERT INTO catalogue (content, source_class, confidence_score, "
            "domain, created_at) VALUES ('model said so', NULL, 0.9, 'GENERAL', ?)",
            ("2026-08-20T00:00:00+00:00",))
        await conn.commit()
        sources = await wiki._get_uncompiled_sources(conn)
        ev = [x for x in sources if x["type"] == "evidence"]
        assert len(ev) == 1
        assert ev[0]["provenance_class"] == "INFERRED"
        assert ev[0]["confidence"] <= INFERRED_CAP

    @pytest.mark.asyncio
    async def test_catalogue_unknown_class_string_fails_closed(self, db):
        conn, wiki = db
        await conn.execute(
            "INSERT INTO catalogue (content, source_class, confidence_score, "
            "domain, created_at) VALUES ('typo class', 'primary ', 0.9, 'GENERAL', ?)",
            ("2026-08-20T00:00:00+00:00",))
        await conn.commit()
        sources = await wiki._get_uncompiled_sources(conn)
        ev = [x for x in sources if x["type"] == "evidence"]
        assert len(ev) == 1
        assert ev[0]["provenance_class"] == "INFERRED"
        assert ev[0]["confidence"] <= INFERRED_CAP


class TestArticleClassLabeling:
    def test_weakest_class_ordering(self):
        from tools.knowledge_wiki import _weakest_source_class
        assert _weakest_source_class([]) is None
        assert _weakest_source_class([{"provenance_class": None}]) is None
        assert _weakest_source_class(
            [{"provenance_class": "PRIMARY"}, {"provenance_class": "INFERRED"}]
        ) == "INFERRED"
        assert _weakest_source_class(
            [{"provenance_class": "SIGNAL"}, {"provenance_class": "SECONDARY"}]
        ) == "SIGNAL"

    @pytest.mark.asyncio
    async def test_created_article_persists_weakest_source_class(self, db):
        """The documented red-team gap: two INFERRED items and two PRIMARY
        items were indistinguishable in the compiled article. The article
        must carry its weakest source class."""
        conn, wiki = db
        sources = [
            {"type": "learning", "id": "g1", "query": "g", "domain": "GENERAL",
             "content": "guess one", "confidence": 0.55,
             "provenance_class": "INFERRED", "timestamp": "2026-08-20T00:00:00+00:00"},
            {"type": "evidence", "id": "e1", "query": "", "domain": "GENERAL",
             "content": "fact", "confidence": 0.9,
             "provenance_class": "PRIMARY", "timestamp": "2026-08-20T00:00:00+00:00"},
        ]
        async def _fake_compile(topic, sources, existing_content):
            return {"title": "t", "summary": "s", "content": "c",
                    "related_topics": []}
        orig = wiki._llm_compile
        wiki._llm_compile = _fake_compile
        try:
            await wiki._create_article(conn, "mixed_topic", sources)
        finally:
            wiki._llm_compile = orig
        art = await wiki._get_article(conn, "mixed_topic")
        assert art is not None
        assert art["provenance_class"] == "INFERRED"

    @pytest.mark.asyncio
    async def test_updated_article_class_merges_to_weakest(self, db):
        conn, wiki = db
        sources = [
            {"type": "evidence", "id": "e1", "query": "", "domain": "GENERAL",
             "content": "fact", "confidence": 0.9,
             "provenance_class": "PRIMARY", "timestamp": "2026-08-20T00:00:00+00:00"}]
        async def _fake_compile(topic, srcs, existing_content):
            return {"title": "t", "summary": "s", "content": "c",
                    "related_topics": []}
        orig = wiki._llm_compile
        wiki._llm_compile = _fake_compile
        try:
            await wiki._create_article(conn, "cls_topic", sources)
            art = await wiki._get_article(conn, "cls_topic")
            assert art["provenance_class"] == "PRIMARY"
            new_src = [{"type": "learning", "id": "g1", "query": "g",
                        "domain": "GENERAL", "content": "guess",
                        "confidence": 0.55, "provenance_class": "INFERRED",
                        "timestamp": "2026-08-21T00:00:00+00:00"}]
            await wiki._update_article(conn, "cls_topic", art, new_src)
        finally:
            wiki._llm_compile = orig
        art = await wiki._get_article(conn, "cls_topic")
        assert art["provenance_class"] == "INFERRED"


class TestLessonWriteMergePolicy:
    @pytest.mark.asyncio
    async def test_lesson_update_never_raises_confidence(self, db):
        """write_lesson_article REPLACED the stored confidence with the
        caller's number on update — a self-reported 0.95 could raise an
        article sitting at 0.3. Updates must merge downward only."""
        conn, wiki = db
        r1 = await wiki.write_lesson_article(
            conn, topic="t_merge", title="first", content="lesson one",
            domain="SIGNAL", confidence=0.30)
        assert r1["action"] == "created"
        r2 = await wiki.write_lesson_article(
            conn, topic="t_merge", title="second", content="lesson two",
            domain="SIGNAL", confidence=0.95)
        assert r2["action"] == "updated"
        art = await wiki._get_article(conn, "t_merge")
        assert art["confidence"] <= 0.30 + 1e-9

    @pytest.mark.asyncio
    async def test_lesson_same_value_rewrite_is_stable(self, db):
        """The autonomous loop rewrites the same lesson topics each cycle
        with constant confidence values — repeated identical writes must
        not ratchet the number either way."""
        conn, wiki = db
        for i in range(3):
            await wiki.write_lesson_article(
                conn, topic="t_stable", title=f"w{i}", content="same lesson",
                domain="SIGNAL", confidence=0.65)
        art = await wiki._get_article(conn, "t_stable")
        assert art["confidence"] == pytest.approx(0.65)

    @pytest.mark.asyncio
    async def test_lesson_create_with_explicit_class_clamps(self, db):
        """Declaring a provenance class on a direct write subjects the
        number to that class's ceiling."""
        conn, wiki = db
        await wiki.write_lesson_article(
            conn, topic="t_cls", title="t", content="c",
            domain="SIGNAL", confidence=0.9, source_class="SIGNAL")
        art = await wiki._get_article(conn, "t_cls")
        assert art["confidence"] <= MAX_CONFIDENCE_BY_SOURCE["SIGNAL"] + 1e-9
        assert art["provenance_class"] == "SIGNAL"


class TestShortCircuitHonesty:
    @pytest.mark.asyncio
    async def test_zero_earned_article_does_not_answer_tasks(self, db, monkeypatch):
        """An article whose confidence is 0/absent earned nothing; it must
        not complete a research task. Previously ``or 0.5`` raised it to a
        passing score and the task was minted COMPLETED."""
        import api
        conn, wiki = db
        fake = _FakeWiki([
            {"topic": "empty", "title": "t", "summary": "s", "content": "c",
             "domain": "GENERAL", "confidence": 0.0, "updated_at": "now",
             "similarity": 0.97},
        ])
        monkeypatch.setattr("tools.knowledge_wiki.get_wiki", lambda *a, **k: fake)
        monkeypatch.setattr(api, "memory", _MemoryStub(_tmp_db_path(db)))
        result = await api._wiki_task_short_circuit("some question")
        assert result is None

    @pytest.mark.asyncio
    async def test_short_circuit_propagates_true_confidence_and_class(self, db, monkeypatch):
        import api
        conn, wiki = db
        fake = _FakeWiki([
            {"topic": "real", "title": "t", "summary": "s", "content": "c",
             "domain": "GENERAL", "confidence": 0.55,
             "provenance_class": "INFERRED", "updated_at": "now",
             "similarity": 0.97},
        ])
        monkeypatch.setattr("tools.knowledge_wiki.get_wiki", lambda *a, **k: fake)
        monkeypatch.setattr(api, "memory", _MemoryStub(_tmp_db_path(db)))
        result = await api._wiki_task_short_circuit("some question")
        assert result is not None
        assert result["confidence_score"] == pytest.approx(0.55)
        assert result["wiki_provenance_class"] == "INFERRED"


class _MemoryStub:
    """api.memory is a module global that is None until app startup; the
    short-circuit probe only touches ``db_path``."""
    def __init__(self, db_path: str):
        self.db_path = db_path


def _tmp_db_path(db_fixture_tuple):
    """A throwaway sqlite path for the short-circuit probe connection."""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


class _FakeWiki:
    def __init__(self, hits):
        self._hits = hits
        self.db_path = ":memory:"

    async def search(self, db, query, top_k=10, domain=None,
                     min_similarity=0.0, limit=None):
        return self._hits
