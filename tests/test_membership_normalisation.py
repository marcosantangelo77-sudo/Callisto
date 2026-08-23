"""Membership-normalisation regression: EVERY public entry point that asks
"is this source in an independence family?" must use the canonical rule
(strip non-alphanumerics, lowercase), so 'semantic_scholar',
'semanticscholar' and 'Semantic-Scholar' are ONE source.

This defect landed three times because each fix touched one call site while
another copy kept the raw `in members` test — which reads two dependent
sources as two INDEPENDENT voices and inflates confidence. One test per
entry point; a single test over one call site is what let it recur.
"""
from __future__ import annotations

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from types import SimpleNamespace  # noqa: E402

from tools.sources.base import independence_family  # noqa: E402
from tools.pipeline.retrieval import in_family, independence_key  # noqa: E402
from tools.why import independence_from_fetches  # noqa: E402


# ── entry point 1: tools.sources.base.independence_family ──────────────────

def test_base_independence_family_matches_unnormalised_name():
    # The family declares its member as 'semantic_scholar'-style names;
    # a source arriving as 'Semantic-Scholar' or 'semantic_scholar' is the
    # SAME source under the canonical rule and must collapse into
    # 'scholarly-aggregator', not stand alone.
    for variant in ("Semantic-Scholar", "semantic_scholar", "SEMANTICSCHOLAR"):
        assert independence_family(variant) == "scholarly-aggregator", (
            f"independence_family({variant!r}) did not collapse to the "
            "declared family — unnormalised membership test is back")
    # And a genuinely unrelated source stands alone.
    assert independence_family("gdelt") == "gdelt"


# ── entry point 2: tools.pipeline.retrieval.in_family ──────────────────────

def test_retrieval_in_family_normalises_both_sides():
    # The canonical rule normalises BOTH the source name and the member.
    assert in_family("Semantic-Scholar", ("semantic_scholar",))
    assert in_family("semantic_scholar", ("Semanticscholar",))
    assert not in_family("gdelt", ("semantic_scholar",))


# ── entry point 3: tools.pipeline.retrieval.independence_key ───────────────

def test_retrieval_independence_key_collapses_variants():
    key = independence_key("openalex", "")
    for variant in ("Semantic-Scholar", "semantic_scholar",
                    "semanticscholar"):
        assert independence_key(variant, "") == key, (
            f"independence_key({variant!r}) fell through to its own unit — "
            "two dependent sources would count as two independent voices")
    assert independence_key("gdelt", "") != key


# ── entry point 4: tools.why.independence_from_fetches ─────────────────────

def test_why_independence_from_fetches_collapses_variants():
    fetch = lambda n: SimpleNamespace(source_name=n, url="")
    why = independence_from_fetches(
        [fetch("openalex"), fetch("Semantic-Scholar")])
    # Both fetches are one family member pair: exactly ONE independent key,
    # and the collapse is spelled out rather than silently missed.
    assert why.n_independent == 1, (
        "'Semantic-Scholar' counted separately from 'openalex' — "
        "inflated independence in the explanation layer")
    assert any("collapse" in c.lower() for c in why.collapses)
