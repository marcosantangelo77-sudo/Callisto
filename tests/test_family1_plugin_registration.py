"""RED TEAM FAMILY 1 fix — the domain plugins that never ran.

PATTERNS.md family 1: "a layer that never actually runs ... Grep for the
verifier's name; if nothing outside its own tests calls it, that is the
bug." finance, kalshi and sources each shipped a register_if_available()
seam whose ONLY caller was its own build test. orchestrator._default_registry()
registered sports + compute; the other three plugins existed, looked
registered (finance's docstring even claimed it), and reached no session.

These tests pin the production registration: a FINANCIAL session gets the
EDGAR tools, a prediction-market query gets kalshi_market_edge, a source
question gets the registry tools, and a broken plugin degrades to absent
instead of killing the loop.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def seeded_registry():
    """Fresh registry seeded by the REAL production seeding path."""
    import orchestrator as o
    from tools.domain_registry import ToolRegistry

    saved_get = o.get_tool_registry
    fresh = ToolRegistry()
    o.get_tool_registry = lambda: fresh
    o._registry_seeded = False
    try:
        assert o._default_registry() is fresh  # runs the true seed logic
        yield fresh
    finally:
        o.get_tool_registry = saved_get
        o._registry_seeded = False


class TestProductionRegistration:
    def test_all_built_plugins_registered(self, seeded_registry):
        names = {p.name for p in seeded_registry.plugins()}
        assert {"sports", "compute", "finance", "kalshi",
                "sources"} <= names

    def test_financial_session_gets_edgar_tools(self, seeded_registry):
        from agp import Domain
        names = seeded_registry.tool_names_for(Domain.FINANCIAL,
                                               "analyze Apple Inc.")
        assert {"edgar_get_statements", "edgar_build_model",
                "edgar_anomalies"} <= names

    def test_prediction_market_query_gets_edge_tool(self, seeded_registry):
        # The edge-quantification lifecycle stage is only reachable if this
        # tool joins the session: previously unreachable at any runtime.
        names = seeded_registry.tool_names_for(
            None, "what is the market saying about the next CPI print")
        assert "kalshi_market_edge" in names

    def test_source_question_gets_registry_tools(self, seeded_registry):
        names = seeded_registry.tool_names_for(
            None, "which source has unemployment rate data")
        assert any(n.startswith("source_") for n in names)

    def test_sports_tools_still_present_alongside(self, seeded_registry):
        # Sports stays green: keyword match still yields the odds toolkit.
        names = seeded_registry.tool_names_for(None, "nba odds for tonight")
        assert len(names) >= 23


class TestRegistrationFailureIsNotFatal:
    def test_broken_plugin_degrades_to_absent(self, seeded_registry):
        """A plugin whose registration raises must not take down the loop."""
        from tools.domain_registry import ToolRegistry
        import orchestrator as o

        reg = ToolRegistry()

        def _boom(_reg):
            raise RuntimeError("plugin exploded")

        o._try_register(reg, _boom, "broken")  # must not raise
        assert not reg.plugins()

    def test_register_if_available_idempotent(self, seeded_registry):
        from tools.domains.kalshi.plugin import register_if_available
        before = len(seeded_registry.plugins())
        assert register_if_available(seeded_registry) is True
        after = register_if_available(seeded_registry)
        assert after is True
        assert len(seeded_registry.plugins()) == before
