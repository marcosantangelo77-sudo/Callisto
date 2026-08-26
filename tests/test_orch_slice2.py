"""Tests for the slice-2 orchestrator extraction (tools/orch/session_steps.py).

Contract under test:
  - orchestrator.Orchestrator still exposes the full step-method surface.
  - Step bodies really live in tools/orch/session_steps.py now.
  - Confidence gates survive the move:
      * INFERRED ceiling 0.55, SECONDARY 0.75 (via _clamp_confidence).
      * Manager can only adjust confidence DOWN; final clamp re-applied.
      * Contradiction penalties: CRITICAL -0.15, MAJOR -0.05, floor respected.
      * Claude enhancement with an empty provenance ledger is fail-closed:
        unverified citations stay INFERRED-tier and never raise confidence
        above the INFERRED ceiling.
  - DOMAIN_SCHEMA and domain_search_query behavior unchanged.
"""

import asyncio

import pytest


@pytest.fixture()
def orch():
    """A real Orchestrator without touching live model loaders.

    Orchestrator.__init__ calls get_architect/get_manager/get_sentinel; we
    bypass construction via object.__new__ and set only what the tested
    code paths need. This keeps the tests hermetic (no Ollama, no Claude).
    """
    from agp.provenance import ProvenanceLedger
    from orchestrator import Orchestrator

    from types import SimpleNamespace

    o = object.__new__(Orchestrator)
    o._active_sessions = {}
    o._provenance = ProvenanceLedger()
    # Duck-typed agents: never constructed for real (no Ollama, no Claude).
    o.architect = SimpleNamespace(config=SimpleNamespace(system_prompt="sys"))
    o.manager = None
    o.sentinel = None
    return o


def _make_session(query="test query?"):
    from agp import AGPSession

    return AGPSession(query)


# ── Facade stability ──────────────────────────────────────────────────────


def test_orchestrator_import_path_stable():
    from orchestrator import Orchestrator  # api.py contract

    assert hasattr(Orchestrator, "run_session")
    assert hasattr(Orchestrator, "active_session_for")


def test_step_methods_still_on_class():
    from orchestrator import Orchestrator

    for name in (
        "_domain_search_query",
        "_architect_system_prompt",
        "_run_searches_parallel",
        "_execute_tool",
    ):
        assert callable(getattr(Orchestrator, name)), name


def test_step_bodies_live_in_tools_orch():
    import inspect

    import orchestrator as o
    from tools.orch import session_steps as ss

    # The class method delegates — its body should be a thin hop, not a copy.
    from orchestrator import Orchestrator

    body = inspect.getsource(Orchestrator._execute_tool)
    assert len(body) < 400  # real body lives in session_steps
    assert "escalate_with_ladder" in inspect.getsource(ss.execute_tool)


def test_facade_reexports_slice2_names():
    import orchestrator as o

    for name in (
        "DOMAIN_SCHEMA",
        "architect_system_prompt",
        "load_session_cache",
        "step_assign_domain",
        "step_collect_evidence",
        "step_check_contradictions",
        "step_synthesize",
        "step_escalate_to_claude",
        "step_manager_review",
        "claude_available",
        "get_cache_manager",
        "MAX_CONFIDENCE_BY_SOURCE",
    ):
        assert hasattr(o, name), name


# ── Domain schema / refinement ────────────────────────────────────────────


def test_domain_schema_unchanged():
    from orchestrator import DOMAIN_SCHEMA

    assert DOMAIN_SCHEMA["required"] == ["domain"]
    assert set(DOMAIN_SCHEMA["properties"]["domain"]["enum"]) == {
        "FINANCIAL", "TECHNICAL", "SIGNAL", "SYNTHESIS", "GENERAL",
    }


def test_domain_search_query_refinements():
    from agp import Domain
    from tools.orch.session_steps import domain_search_query

    q = "nvidia earnings\nsecond line ignored"
    out = domain_search_query(q, Domain.FINANCIAL)
    assert "market analysis financial data" in out
    assert "\n" not in out and len(out) < 250
    assert "research breakthrough" in domain_search_query(q, Domain.TECHNICAL)
    assert "trend indicator" in domain_search_query(q, Domain.SIGNAL)
    assert domain_search_query(q, Domain.GENERAL) is None


# ── Manager review gates ──────────────────────────────────────────────────


class _FakeManager:
    """Returns the inference-response shape step_manager_review expects."""

    def __init__(self, parsed):
        self._parsed = parsed

    async def achat(self, messages):
        return {"parsed_json": self._parsed}


def _Ev(sc="INFERRED", content="c", conf=0.5):
    """Real AGP Evidence — add_evidence filters on confidence_tier."""
    from agp import Domain, Evidence, SourceClass

    return Evidence(
        content=content,
        source_class=SourceClass(sc),
        confidence_score=conf,
        domain=Domain.GENERAL,
        origin_agent="test",
    )


def _run_manager_review(orch, session, summary, used_tools, parsed):
    from unittest.mock import patch

    orch.manager = _FakeManager(parsed)

    async def run():
        from tools.orch.session_steps import step_manager_review
        return await step_manager_review(orch, session, summary, used_tools)

    return asyncio.run(run())


def test_manager_cannot_raise_confidence(orch):
    from agp import AGPSession, SessionSummary

    from agp import Domain

    session = AGPSession("q")
    session.domain = Domain.GENERAL
    session.add_evidence(_Ev())
    summary = SessionSummary(
        scope="q", domain=session.domain, conclusion="c",
        confidence_score=0.40, evidence_count=1, contradiction_count=0,
    )
    out = _run_manager_review(
        orch, session, summary, False,
        {"approved": True, "adjusted_confidence": 0.95, "objections": []},
    )
    # Manager tried 0.95 — must be refused (down-only), then clamp keeps ≤0.55
    assert out.confidence_score <= 0.55


def test_manager_downward_adjustment_applied_then_clamped(orch):
    from agp import AGPSession, Domain, SessionSummary

    session = AGPSession("q")
    session.domain = Domain.GENERAL
    session.add_evidence(_Ev())
    summary = SessionSummary(
        scope="q", domain=session.domain, conclusion="c",
        confidence_score=0.50, evidence_count=1, contradiction_count=0,
    )
    out = _run_manager_review(
        orch, session, summary, False,
        {"approved": True, "adjusted_confidence": 0.30, "objections": ["weak"]},
    )
    assert out.confidence_score == 0.30
    assert session.manager_objections == ["weak"]


def test_contradiction_penalty_critical_and_major(orch):
    from agp import AGPSession, Contradiction, Domain, SessionSummary

    session = AGPSession("q")
    session.domain = Domain.GENERAL
    session.add_evidence(_Ev())
    session.add_contradiction(Contradiction(
        claim_a="a", claim_b="b", source_a="x", source_b="y",
        severity="CRITICAL", resolution="",
    ))
    session.add_contradiction(Contradiction(
        claim_a="c", claim_b="d", source_a="x", source_b="z",
        severity="MAJOR", resolution="",
    ))
    summary = SessionSummary(
        scope="q", domain=session.domain, conclusion="c",
        confidence_score=0.50, evidence_count=1, contradiction_count=2,
    )
    out = _run_manager_review(orch, session, summary, False, None)
    # 0.50 - 0.15 (CRITICAL) - 0.05 (MAJOR) = 0.30
    assert out.confidence_score == pytest.approx(0.30)


def test_contradiction_penalty_respects_db_floor(orch):
    from agp import AGPSession, Contradiction, Domain, SessionSummary
    from agp.thresholds import DB_CONFIDENCE_FLOOR

    session = AGPSession("q")
    session.domain = Domain.GENERAL
    session.add_evidence(_Ev())
    for sev in ("CRITICAL", "CRITICAL"):
        session.add_contradiction(Contradiction(
            claim_a="a", claim_b="b", source_a="x", source_b="y",
            severity=sev, resolution="",
        ))
    summary = SessionSummary(
        scope="q", domain=session.domain, conclusion="c",
        confidence_score=0.20, evidence_count=1, contradiction_count=2,
    )
    out = _run_manager_review(orch, session, summary, False, None)
    assert out.confidence_score == DB_CONFIDENCE_FLOOR


def test_no_contradictions_no_penalty(orch):
    from agp import AGPSession, Domain, SessionSummary

    session = AGPSession("q")
    session.domain = Domain.GENERAL
    session.add_evidence(_Ev("SECONDARY", conf=0.7))
    summary = SessionSummary(
        scope="q", domain=session.domain, conclusion="c",
        confidence_score=0.70, evidence_count=1, contradiction_count=0,
    )
    out = _run_manager_review(orch, session, summary, False, None)
    assert out.confidence_score == pytest.approx(0.70)


# ── Claude enhancement fail-closed citation grounding ─────────────────────


def _run_escalate(orch, session, summary, ladder_result):
    from unittest.mock import patch

    async def fake_available():
        return True

    async def fake_ladder(*a, **kw):
        return ladder_result

    async def run():
        from tools.orch.session_steps import step_escalate_to_claude
        with patch("tools.orch.session_steps.claude_code_available", fake_available), \
             patch("tools.orch.session_steps.escalate_with_ladder", fake_ladder):
            return await step_escalate_to_claude(orch, session, summary)

    return asyncio.run(run())


def test_escalation_unverified_citation_stays_inferred_tier(orch):
    """Empty ledger (fail-closed): a response printing an http:// URL but not
    parsed as JSON must NOT earn the SECONDARY ceiling."""
    from agp import AGPSession, SessionSummary
    from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE

    session = AGPSession("q")
    summary = SessionSummary(
        scope="q", domain=None, conclusion="local guess",
        confidence_score=0.40, evidence_count=0, contradiction_count=0,
    )
    result = {
        "content": (
            "See http://example.com/real-data for proof. The answer is "
            "definitely yes with overwhelming support across many sources."
        ),
        "model": "test-model",
    }
    out, escalated = _run_escalate(orch, session, summary, result)
    assert escalated is True
    ev = session.evidence[-1]
    # Unverified cite ⇒ INFERRED tier, clamped to the INFERRED ceiling.
    assert ev.source_class.value == "INFERRED"
    assert "[uncited]" in ev.source_name
    assert out.confidence_score <= MAX_CONFIDENCE_BY_SOURCE["INFERRED"]


def test_escalation_parsed_json_unverified_stays_clamped(orch):
    from agp import AGPSession, SessionSummary
    from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE

    session = AGPSession("q")
    summary = SessionSummary(
        scope="q", domain=None, conclusion="local guess",
        confidence_score=0.40, evidence_count=0, contradiction_count=0,
    )
    result = {
        "content": (
            '{"conclusion":"enhanced analysis","confidence_score":0.95,'
            '"key_findings":["k"],"gaps":[]}'
        ),
        "model": "test-model",
    }
    out, escalated = _run_escalate(orch, session, summary, result)
    assert escalated is True
    ev = session.evidence[-1]
    assert ev.source_class.value == "INFERRED"
    assert out.confidence_score <= MAX_CONFIDENCE_BY_SOURCE["INFERRED"]
    assert out.conclusion == "enhanced analysis"


def test_escalation_skipped_when_cli_unavailable(orch):
    from agp import AGPSession, SessionSummary
    from unittest.mock import patch

    session = AGPSession("q")
    summary = SessionSummary(
        scope="q", domain=None, conclusion="keep",
        confidence_score=0.40, evidence_count=0, contradiction_count=0,
    )

    async def fake_available():
        return False

    async def run():
        from tools.orch.session_steps import step_escalate_to_claude
        with patch("tools.orch.session_steps.claude_code_available", fake_available):
            return await step_escalate_to_claude(orch, session, summary)

    out, escalated = asyncio.run(run())
    assert escalated is False
    assert out.conclusion == "keep"
    assert not session.evidence


def test_escalation_error_returns_summary_unchanged(orch):
    from agp import AGPSession, SessionSummary

    session = AGPSession("q")
    summary = SessionSummary(
        scope="q", domain=None, conclusion="keep",
        confidence_score=0.40, evidence_count=0, contradiction_count=0,
    )
    out, escalated = _run_escalate(
        orch, session, summary, {"error": "cli missing", "content": ""}
    )
    assert escalated is False
    assert out.conclusion == "keep"


# ── Contradiction parsing ─────────────────────────────────────────────────


def test_check_contradictions_empty_evidence_short_circuits(orch):
    from agp import AGPSession

    session = AGPSession("q")

    async def run():
        from tools.orch.session_steps import step_check_contradictions
        return await step_check_contradictions(orch, session, "")

    assert asyncio.run(run()) == []


# ── Synthesis clamping ────────────────────────────────────────────────────


def _run_synthesize(orch, session, used_tools, arch_prompt="sys"):
    async def run():
        from tools.orch.session_steps import step_synthesize
        return await step_synthesize(orch, session, used_tools, arch_prompt)

    return asyncio.run(run())


class _FakeArchitect:
    def __init__(self, response):
        self._response = response
        self.config = type("C", (), {"system_prompt": "sys"})()

    async def achat(self, messages, **kw):
        return self._response


def test_synthesize_local_path_clamps_inferred_confidence(orch):
    from agp import AGPSession
    from agp.thresholds import MAX_CONFIDENCE_NO_TOOL

    orch.architect = _FakeArchitect(
        {"content": '{"conclusion":"big claim","confidence_score":0.99}'}
    )
    session = AGPSession("q")
    out = _run_synthesize(orch, session, used_tools=False)
    assert out.confidence_score <= MAX_CONFIDENCE_NO_TOOL


def test_synthesize_malformed_json_yields_floor_confidence(orch):
    from agp import AGPSession
    from agp import EMPTY_SYNTHESIS_MARKER

    orch.architect = _FakeArchitect({"content": "not json at all"})
    session = AGPSession("q")
    out = _run_synthesize(orch, session, used_tools=False)
    assert out.conclusion == EMPTY_SYNTHESIS_MARKER
    assert out.confidence_score <= 0.55


# ── Tool dispatch delegation ──────────────────────────────────────────────


def test_facade_execute_tool_delegates_to_session_steps():
    import orchestrator as o
    from tools.orch import session_steps as ss

    assert o.execute_tool.__module__ == ss.__name__

# ── run_session_flow extraction (slice 2b) ─────────────────────────────────


def test_run_session_flow_lives_in_tools_orch():
    import inspect

    import orchestrator as o
    from tools.orch import session_steps as ss

    # The real pipeline body lives in session_steps.run_session_flow.
    src = inspect.getsource(ss.run_session_flow)
    assert "step_collect_evidence" in src
    assert "step_synthesize" in src
    assert "step_manager_review" in src
    assert "session.seal()" in src
    # Orchestrator.run_session is now a thin hop.
    facade = inspect.getsource(o.Orchestrator.run_session)
    assert "run_session_flow" in facade
    assert len(facade) < 600


def test_run_session_flow_seal_refused_fail_closed(orch):
    """Seal refusal must return the unsealed/stored=False shape — never a
    forged 'sealed' payload."""
    from unittest.mock import patch

    from agp import AGPSealRefused, AGPSession, Domain, SessionSummary

    async def run():
        session = AGPSession("q")
        session.domain = Domain.GENERAL
        summary = SessionSummary(
            scope="q", domain=Domain.GENERAL, conclusion="c",
            confidence_score=0.5, evidence_count=1, contradiction_count=0,
        )

        async def fake_seal():
            raise AGPSealRefused("majority of evidence filtered")

        with patch("tools.orch.session_steps.load_session_cache",
                   new=_async_return("")), \
             patch("tools.orch.session_steps.step_assign_domain",
                   new=_async_return(Domain.GENERAL)), \
             patch("tools.orch.session_steps.step_enumerate_sources",
                   new=_async_return([])), \
             patch("tools.orch.session_steps.step_collect_evidence",
                   new=_async_return(([], False))), \
             patch("tools.orch.session_steps.step_check_contradictions",
                   new=_async_return([])), \
             patch("tools.orch.session_steps.step_synthesize",
                   new=_async_return(summary)), \
             patch("tools.orch.session_steps.step_escalate_to_claude",
                   new=_async_return((summary, False))), \
             patch("tools.orch.session_steps.step_manager_review",
                   new=_async_return(summary)), \
             patch.object(AGPSession, "seal", side_effect=AGPSealRefused("no")):
            from tools.orch.session_steps import run_session_flow
            return await run_session_flow(orch, "q", skip_search=True)

    out = asyncio.run(run())
    assert out["sealed"] is False
    assert out["stored"] is False
    assert out["error"] == "seal_refused"
    assert "seal_refused_reason" in out


class _AsyncReturn:
    def __init__(self, value):
        self._value = value

    async def __call__(self, *a, **kw):
        return self._value


def _async_return(value):
    r = _AsyncReturn(value)

    async def _inner(*a, **kw):
        return await r(*a, **kw) if False else value

    return _inner


def test_run_session_flow_happy_path_seals_and_stores(orch):
    """Full flow with stubbed steps: seals, stores, and reports success."""
    from unittest.mock import patch

    from agp import AGPSession, Domain, Evidence as RealEv, SessionSummary, SourceClass

    class _Mem:
        def __init__(self):
            self.stored_sessions = []

        async def store_evidence(self, sid, ev):
            pass

        async def store_session(self, session):
            self.stored_sessions.append(session)

    mem = _Mem()
    orch.memory = mem

    async def run():
        session = AGPSession("q")
        session.domain = Domain.GENERAL
        ev = RealEv(
            content="c", source_class=SourceClass.INFERRED,
            confidence_score=0.5, domain=Domain.GENERAL, origin_agent="t",
        )
        summary = SessionSummary(
            scope="q", domain=Domain.GENERAL, conclusion="done",
            confidence_score=0.5, evidence_count=1, contradiction_count=0,
        )
        with patch("tools.orch.session_steps.load_session_cache",
                   new=_async_return("")), \
             patch("tools.orch.session_steps.step_assign_domain",
                   new=_async_return(Domain.GENERAL)), \
             patch("tools.orch.session_steps.step_enumerate_sources",
                   new=_async_return([])), \
             patch("tools.orch.session_steps.step_collect_evidence",
                   new=_async_return(([ev], True))), \
             patch("tools.orch.session_steps.step_check_contradictions",
                   new=_async_return([])), \
             patch("tools.orch.session_steps.step_synthesize",
                   new=_async_return(summary)), \
             patch("tools.orch.session_steps.step_escalate_to_claude",
                   new=_async_return((summary, False))), \
             patch("tools.orch.session_steps.step_manager_review",
                   new=_async_return(summary)):
            from tools.orch.session_steps import run_session_flow
            return await run_session_flow(orch, "q", skip_search=True)

    out = asyncio.run(run())
    assert out["sealed"] is True
    assert out["stored"] is True
    assert len(mem.stored_sessions) == 1
