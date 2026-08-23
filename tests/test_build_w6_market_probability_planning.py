"""Market-implied probability must be plannable — the smoke5 lesson.

All five smoke5 retrodiction questions ("will X beat consensus?") selected
kalshi and polymarket — the only sources carrying market-implied
probability — but build_plan had no planner for either, so the retrieval
fan-out silently skipped them and the pipeline answered market questions
from academic-paper metadata. The adversary called it "tool-selection
failure" on every question. These tests pin the seam:

1. every source the registry SELECTS for a question is either plannable or
   an HONEST GAP with a stated reason — never 'unknown source';
2. polymarket plans a real Gamma public-search call;
3. kalshi passes explicit tickers through and refuses topics honestly.

No live API calls (no-socket guard); the public_search endpoint itself was
verified by hand against the live Gamma API and recorded in the commit.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers.no_socket import NoSocket  # noqa: E402

_guard = NoSocket()
_guard.install()

from tools.sources.query_builder import (  # noqa: E402
    honest_gaps,
    plannable_sources,
    build_plan,
)
from tools.sources.registry import get_source_registry  # noqa: E402

CONSENSUS_Q = ("Will Apple report quarterly results above Wall Street "
               "consensus expectations in its next earnings report?")


def test_registry_selected_sources_are_never_unknown_to_the_planner():
    """THE regression. For a battery of real questions, anything select()
    returns must be in planners ∪ honest_gaps. 'unknown source' inside the
    fan-out is the silent dead end that produced the smoke5 Brier."""
    reg = get_source_registry()
    planned, gaps = set(plannable_sources()), set(honest_gaps())
    known = planned | gaps
    questions = [
        CONSENSUS_Q,
        "Will the Fed cut rates at the next FOMC meeting?",
        "What does recent research say about semiconductor supply chains?",
        "Is unemployment rising faster than consensus expects?",
        "Will CPI inflation come in above forecasts this month?",
    ]
    for q in questions:
        for spec in reg.select(q):
            assert spec.name in known, (
                f"registry selects {spec.name!r} but query authoring has "
                f"no planner and no honest gap for it")


def test_polymarket_plans_public_search():
    p = build_plan("polymarket", CONSENSUS_Q)
    assert p.plannable
    q = p.queries[0]
    assert q.source == "polymarket" and q.method == "public_search"
    core = q.kwargs["query"]
    assert "apple" in core.lower() and "consensus" in core.lower()
    # market vocabulary stripped; topical words kept
    assert "market" not in core.lower().split()
    json.dumps(p.to_dict())


def test_polymarket_refuses_unsearchable_question():
    assert build_plan("polymarket", "What is it?").plannable is False


def test_kalshi_explicit_ticker_passes_through():
    p = build_plan("kalshi", "What is the current price on KXCPI-26SEP-T35?")
    assert p.plannable
    assert p.queries[0].method == "get_market"
    assert p.queries[0].args == ("KXCPI-26SEP-T35",)


def test_kalshi_topic_question_is_an_honest_refusal():
    p = build_plan("polymarket" if False else "kalshi",
                   "Will the Fed raise rates in September?")
    assert not p.plannable
    assert "no free-text" in p.reason


def test_honest_gap_uses_the_real_registry_name():
    """The SEC gap was keyed 'sec_fts' while the adapter registers as
    'sec_fulltext' — the deliberate refusal degraded to 'unknown source'
    and nobody noticed. Gap keys must match registered sources."""
    reg = get_source_registry()
    for name in honest_gaps():
        assert reg.get(name) is not None, (
            f"honest gap {name!r} matches no registered source")
