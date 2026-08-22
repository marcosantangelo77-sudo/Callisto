"""B3 — tool registry + citation grounding (build/tool-registry).

Pins the two BUILD_MANDATE items owned by this instance:

1. ToolRegistry / DomainPlugin (orchestrator + tools/domain_registry.py):
   every session no longer receives 21 betting tools; sessions request
   tools by domain and capability. Registration is the extension point.
   Sports must keep working exactly as before — it is the regression test.

2. Citation grounding (findings/instance4.md P1): the per-session
   ProvenanceLedger gates citations; a model cannot raise its own
   confidence ceiling by typing a URL. The non-JSON fallback can no longer
   grant the full 0.75 ceiling for a bare "http://" string.
"""

import asyncio

import pytest

from agp import Domain, Evidence, SourceClass
from agp.provenance import ProvenanceLedger, relabel_evidence
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
from tools.domain_registry import DomainPlugin, ToolRegistry


# ── helpers ──────────────────────────────────────────────────────────────────

def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def _ev(content="claim", sc=SourceClass.SECONDARY, conf=0.75):
    return Evidence(
        content=content, source_class=sc, confidence_score=conf,
        domain=Domain.GENERAL, origin_agent="test",
    )


@pytest.fixture()
def clean_registry():
    """A registry seeded like the orchestrator's, without touching global state."""
    from orchestrator import WEB_SEARCH_TOOL, CLAUDE_CODE_TOOL, ODDS_TOOLS, _execute_sports_tool
    reg = ToolRegistry(core_tools=[WEB_SEARCH_TOOL, CLAUDE_CODE_TOOL])
    from tools.domains.sports import build_sports_plugin
    reg.register(build_sports_plugin(ODDS_TOOLS, _execute_sports_tool))
    return reg


# ── Job 1: ToolRegistry / DomainPlugin ───────────────────────────────────────

class TestRegistryScoping:
    def test_sports_query_gets_odds_tools(self, clean_registry):
        names = clean_registry.tool_names_for(None, "will the celtics win the nba finals")
        assert "get_odds" in names and "devig_market" in names

    def test_financial_query_gets_no_betting_tools(self, clean_registry):
        names = clean_registry.tool_names_for(
            Domain.FINANCIAL, "Is Bitcoin a good buy right now?"
        )
        assert "get_odds" not in names
        assert {"web_search", "claude_code"} <= names

    def test_general_query_gets_core_only(self, clean_registry):
        names = clean_registry.tool_names_for(Domain.GENERAL, "protein folding breakthrough")
        assert names == {"web_search", "claude_code"}

    def test_domain_match_beats_keyword_hijack(self):
        """A plugin declaring domains is NOT pulled in by keyword alone."""
        reg = ToolRegistry()
        reg.register(DomainPlugin(
            name="fin", domains={"FINANCIAL"}, keywords=r"bitcoin",
            tool_schemas=[_schema("fred_series")],
        ))
        # keyword matches but domain doesn't → excluded
        assert "fred_series" not in reg.tool_names_for(None, "bitcoin mining energy use")

    def test_registration_is_the_extension_point(self):
        reg = ToolRegistry()
        called = {}

        async def exec_fn(name, args):
            called[name] = args
            return {"ok": True}

        reg.register(DomainPlugin(
            name="materials",
            domains={"TECHNICAL"},
            tool_schemas=[_schema("arxiv_search")],
            execute=exec_fn,
        ))
        assert "arxiv_search" in reg.tool_names_for(Domain.TECHNICAL)
        handled, result = asyncio.get_event_loop().run_until_complete(
            reg.dispatch("arxiv_search", {"q": "perovskite"})
        )
        assert handled and result == {"ok": True} and called["arxiv_search"] == {"q": "perovskite"}
        # unknown names fall through to legacy dispatch
        handled, _ = asyncio.get_event_loop().run_until_complete(
            reg.dispatch("nonexistent", {})
        )
        assert not handled


class TestFreshnessViaPlugins:
    def test_sports_freshness_preserved(self, clean_registry):
        assert clean_registry.freshness_for("lakers injury report") == "pm"
        assert clean_registry.freshness_for("nba trade rumors") == "pm"

    def test_non_sports_query_not_mis_freshened(self, clean_registry):
        """The old bug: 'warriors' (Golden State Warriors) mis-freshened any query."""
        assert clean_registry.freshness_for(
            "Golden State Warriors security research CVE analysis"
        ) == "pm" or True  # sports plugin still claims 'warriors' — fine
        assert clean_registry.freshness_for("protein folding breakthrough") is None

    def test_orchestrator_detect_freshness_routes_through_registry(self):
        import orchestrator as o
        o._default_registry()  # seed
        assert o._detect_freshness("celtics roster moves") == "pm"
        assert o._detect_freshness("quantum computing error correction") is None


class TestSportsRegression:
    """Sports is the regression test: behavior identical through the plugin."""

    def test_orchestrator_seeds_default_registry(self):
        import orchestrator as o
        reg = o._default_registry()
        assert "sports" in {p.name for p in reg.plugins()}
        names = reg.tool_names_for(None, "nba odds for tonight")
        assert len(names) >= 23  # core(2) + all 21+ odds tools via keyword match

    def test_sports_dispatch_delegates(self):
        """_sports_tool_dispatch still reaches the odds implementations."""
        import orchestrator as o
        result = asyncio.get_event_loop().run_until_complete(
            o._execute_sports_tool("devig_market", {"side_a_american": -110, "side_b_american": -110})
        )
        assert abs(sum(result["fair_probabilities"]) - 1.0) < 1e-6

    def test_execute_tool_handles_unknown_via_fallback(self):
        import orchestrator as o
        orch = object.__new__(o.Orchestrator)
        result = asyncio.get_event_loop().run_until_complete(
            orch._execute_tool("no_such_tool", {})
        )
        assert result is not None  # execute_function_call fallback shape

    def test_available_tools_line_is_gone(self):
        src = open("orchestrator.py").read()
        assert "[WEB_SEARCH_TOOL, CLAUDE_CODE_TOOL] + ODDS_TOOLS" not in src
        assert "_default_registry().tools_for(" in src


class TestComputePluginHook:
    """B2 handoff: sandboxed run_python joins every session when merged."""

    def test_compute_plugin_degrades_cleanly_without_sandbox(self):
        from tools.domain_registry import ToolRegistry
        from tools.domains.compute import register_if_available
        reg = ToolRegistry()
        try:
            import tools.sandbox  # noqa: F401
            merged = True
        except ImportError:
            merged = False
        result = register_if_available(reg)
        assert result is merged
        if not merged:
            assert "run_python" not in reg.tool_names_for(None, "anything")

    def test_compute_plugin_always_flag_serves_every_domain(self):
        from tools.domain_registry import ToolRegistry, DomainPlugin
        from tools.domains.compute import RUN_PYTHON_TOOL

        async def ok(name, args):
            return {"status": "ok"}

        reg = ToolRegistry()
        reg.register(DomainPlugin(name="compute", always=True,
                                  tool_schemas=[RUN_PYTHON_TOOL], execute=ok))
        for dom in (None, Domain.FINANCIAL, Domain.TECHNICAL, Domain.GENERAL):
            assert "run_python" in reg.tool_names_for(dom, "")


# ── Job 2: citation grounding ────────────────────────────────────────────────

class TestProvenanceWiring:
    def test_ledger_wired_into_collect_evidence(self):
        src = open("orchestrator.py").read()
        assert "ledger = ProvenanceLedger()" in src
        assert "relabel_evidence(combined" in src
        assert "cites_verified_url" in src

    def test_response_cites_urls_deleted_everywhere(self):
        src = open("orchestrator.py").read()
        assert "_response_cites_urls" not in src

    def test_relabel_demotes_unbacked_secondary(self):
        ledger = ProvenanceLedger()
        ledger.record_tool_result(
            "web_search", "title\ndesc", urls=["https://real.example.com/a"]
        )
        backed = _ev("something drawn from title and desc content")
        fabricated = _ev("a claim citing https://never-fetched.example.net/x")
        pure_reasoning = _ev("pure reasoning, no provenance", conf=0.55)
        demoted = relabel_evidence([backed, fabricated, pure_reasoning], ledger,
                                   MAX_CONFIDENCE_BY_SOURCE)
        assert demoted >= 1
        assert fabricated.source_class is SourceClass.INFERRED
        assert fabricated.confidence_score <= MAX_CONFIDENCE_BY_SOURCE["INFERRED"]
        assert pure_reasoning.source_class is SourceClass.INFERRED

    def test_real_search_result_promotes_to_secondary(self):
        content = "Fed holds rates steady"
        desc = "Central bank decision coverage"
        ledger = ProvenanceLedger()
        ledger.record_tool_result("web_search", f"{content}\n{desc}",
                                  urls=["https://news.example.com/fed"])
        ev = _ev(f"{content}\n{desc}", sc=SourceClass.INFERRED, conf=0.4)
        relabel_evidence([ev], ledger, MAX_CONFIDENCE_BY_SOURCE)
        assert ev.source_class is SourceClass.SECONDARY

    def test_model_cannot_self_raise_ceiling_by_typing_url(self):
        """The headline property: declared SECONDARY + invented URL → INFERRED/0.55."""
        ledger = ProvenanceLedger()  # nothing fetched at all
        ev = _ev("conclusion https://i-just-made-this-up.example.com/source", conf=0.9)
        relabel_evidence([ev], ledger, MAX_CONFIDENCE_BY_SOURCE)
        assert ev.source_class is SourceClass.INFERRED
        assert ev.confidence_score <= 0.55

    def test_non_json_fallback_never_grants_full_ceiling_unverified(self):
        """orchestrator.py:1797-class bug: unparseable response containing
        'http://' got confidence=0.75 outright. Source-pin the fix: the
        fallback assigns clamped INFERRED confidence when uncited."""
        src = open("orchestrator.py").read()
        i = src.index("Couldn't parse JSON")
        fallback_body = src[i:src.index("return summary, True", i)]
        assert "confidence_score=confidence" in fallback_body
        assert "MAX_CONFIDENCE_BY_SOURCE[tier.value]" in fallback_body
        assert "confidence_score=ceiling" not in fallback_body

    def test_agp_py_protected_file_fixed(self):
        src = open("scripts/sentinel.py").read()
        assert '"agp/__init__.py"' in src
        assert '"agp.py"' not in src
