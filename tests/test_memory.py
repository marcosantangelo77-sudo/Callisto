"""Tests for compartmentalized memory — DB constraints, domain isolation, cross-domain logging."""

import pytest
import pytest_asyncio
from agp import Domain, Evidence, SourceClass, AGPSession, SessionStep, SessionSummary
from memory import MemoryStore


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def memory(db_path):
    mem = MemoryStore(db_path)
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.mark.asyncio
class TestMemoryConstraints:
    async def test_unverified_rejected_by_db(self, memory):
        """confidence_score < 0.30 should be rejected by app layer."""
        ev = Evidence(
            content="weak", source_class=SourceClass.INFERRED,
            confidence_score=0.25, domain=Domain.GENERAL, origin_agent="test",
        )
        result = await memory.store_evidence("test-session", ev)
        assert result is None  # filtered by app layer

    async def test_storable_evidence_persists(self, memory):
        ev = Evidence(
            content="solid finding", source_class=SourceClass.SECONDARY,
            confidence_score=0.7, domain=Domain.TECHNICAL, origin_agent="architect",
            source_name="brave_search",
        )
        entry_id = await memory.store_evidence("sess-1", ev)
        assert entry_id is not None
        assert entry_id > 0

    async def test_domain_isolation(self, memory):
        """Evidence stored in TECHNICAL should not appear in FINANCIAL world."""
        ev = Evidence(
            content="tech finding", source_class=SourceClass.SECONDARY,
            confidence_score=0.7, domain=Domain.TECHNICAL, origin_agent="architect",
        )
        await memory.store_evidence("sess-1", ev)

        tech = await memory.query_world(Domain.TECHNICAL)
        fin = await memory.query_world(Domain.FINANCIAL)
        assert len(tech) == 1
        assert len(fin) == 0


@pytest.mark.asyncio
class TestCrossDomainAccess:
    async def test_cross_domain_logged(self, memory):
        ev = Evidence(
            content="tech data", source_class=SourceClass.SECONDARY,
            confidence_score=0.7, domain=Domain.TECHNICAL, origin_agent="architect",
        )
        await memory.store_evidence("sess-1", ev)

        results = await memory.cross_domain_query(
            requesting_agent="manager",
            requesting_domain=Domain.FINANCIAL,
            target_domain=Domain.TECHNICAL,
        )
        assert len(results) == 1

        # Verify audit log was written
        rows = await memory._db.execute_fetchall(
            "SELECT * FROM cross_domain_access_log"
        )
        assert len(rows) == 1
        assert "FINANCIAL" in str(rows[0])
        assert "TECHNICAL" in str(rows[0])


@pytest.mark.asyncio
class TestSessionStorage:
    async def test_sealed_session_stored_and_retrieved(self, memory):
        s = AGPSession("test query")
        s.domain = Domain.GENERAL
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        # Post-rigor seal() requires real evidence + real conclusion.
        s.add_evidence(Evidence(
            content="supporting fact", source_class=SourceClass.SECONDARY,
            confidence_score=0.7, domain=Domain.GENERAL, origin_agent="test",
        ))
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope="test query", domain=Domain.GENERAL,
            conclusion="result", confidence_score=0.5,
            evidence_count=1, contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        s.seal()

        await memory.store_session(s)
        retrieved = await memory.get_session(s.session_id)
        assert retrieved is not None
        assert retrieved["seal_hash"] == s.seal_hash
        assert retrieved["query"] == "test query"

    async def test_get_session_detects_tampered_seal(self, memory):
        """memory.get_session() must recompute+verify the seal and raise
        AGPSealTampered when the stored payload disagrees with seal_hash."""
        import json as _json
        from agp import AGPSealTampered

        s = AGPSession("tamper test")
        s.domain = Domain.GENERAL
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        s.add_evidence(Evidence(
            content="honest fact", source_class=SourceClass.SECONDARY,
            confidence_score=0.7, domain=Domain.GENERAL, origin_agent="test",
        ))
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope="tamper test", domain=Domain.GENERAL,
            conclusion="honest conclusion", confidence_score=0.6,
            evidence_count=1, contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        s.seal()
        await memory.store_session(s)

        # Direct-mutate the stored full_session JSON to simulate tampering
        rows = await memory._db.execute_fetchall(
            "SELECT full_session FROM sessions WHERE session_id = ?",
            (s.session_id,),
        )
        doc = _json.loads(rows[0][0])
        doc["summary"]["conclusion"] = "MALICIOUS REWRITE"
        await memory._db.execute(
            "UPDATE sessions SET full_session = ? WHERE session_id = ?",
            (_json.dumps(doc), s.session_id),
        )
        await memory._db.commit()

        with pytest.raises(AGPSealTampered):
            await memory.get_session(s.session_id)

        # verify=False bypass (for audit tool) should still return data
        untrusted = await memory.get_session(s.session_id, verify=False)
        assert untrusted is not None
        assert untrusted["summary"]["conclusion"] == "MALICIOUS REWRITE"
