"""Tests for the orchestrator.py pipeline-helper split (tools/orch/)."""

import asyncio


def test_orchestrator_import_path_stable():
    from orchestrator import Orchestrator  # api.py contract

    assert hasattr(Orchestrator, "run_session")


def test_facade_reexports():
    import orchestrator as o

    for name in (
        "WEB_SEARCH_TOOL", "CLAUDE_CODE_TOOL", "ODDS_TOOLS",
        "_execute_sports_tool", "_default_registry", "_detect_freshness",
        "_clamp_confidence", "_best_source_class", "_dedup_search_results",
        "_safe_parse", "_parse_domain", "_json_compact", "HERMES_TOOL_PROMPT",
        "MAX_CONFIDENCE_BY_SOURCE", "MAX_CONFIDENCE_NO_TOOL", "ESCALATION_THRESHOLD",
    ):
        assert hasattr(o, name), name


def test_helpers_live_in_tools_orch():
    from tools.orch import pipeline_support as ps
    import inspect
    import tools.orch.sports_dispatch as sd
    import orchestrator as o

    assert ps._clamp_confidence is o._clamp_confidence
    assert callable(sd._sports_tool_dispatch)
    # _sports_tool_dispatch body really lives in tools/orch now
    assert "tools/orch" in sd.__file__
    assert len(inspect.getsource(ps.run_searches_parallel)) > 200


def test_registry_seeds_and_dispatch_works():
    import orchestrator as o

    reg = o._default_registry()
    assert "sports" in {p.name for p in reg.plugins()}
    result = asyncio.run(
        o._execute_sports_tool(
            "devig_market", {"side_a_american": -110, "side_b_american": -110}
        )
    )
    assert abs(sum(result["fair_probabilities"]) - 1.0) < 1e-6


def test_detect_freshness_routes_through_registry():
    import orchestrator as o

    o._default_registry()  # seed
    assert o._detect_freshness("celtics roster moves") == "pm"
    assert o._detect_freshness("quantum computing error correction") is None


def test_execute_tool_fallback_intact():
    import orchestrator as o

    orch = object.__new__(o.Orchestrator)
    result = asyncio.run(orch._execute_tool("no_such_tool", {}))
    assert result is not None


def test_clamp_confidence_enforcement_unchanged():
    from orchestrator import _clamp_confidence

    assert _clamp_confidence(0.9, "INFERRED") == 0.55
    assert _clamp_confidence(0.9, "SECONDARY") == 0.75
    assert _clamp_confidence(1.2, "PRIMARY") == 1.0


def test_seal_call_site_goes_through_keyed_session_seal():
    """Source pin: run_session still seals via AGPSession.seal(), no unkeyed path."""
    import inspect
    import orchestrator as o

    src = inspect.getsource(o.Orchestrator.run_session)
    assert "session.seal()" in src
    assert "AGPSealRefused" in src


def test_orchestrator_shrunk():
    lines = open("orchestrator.py").read().count("\n")
    assert lines < 1100, f"orchestrator.py should be shrunk, has {lines} lines"
