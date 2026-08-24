"""Standing review run 11 (2026-08-23 late) — newest merges, families #1/#3.

Under review: build/information-gain, build/source-health,
build/derived-analysis-loop, honest-null gap-verdict wiring — the four
merges no prior review run examined.

DELIVERABLE REPROS (fail on current master by construction):

  RV11-A  family #3 inside the anti-family-#3 module: the source-health
          USPTO probe maps a ZERO-result payload to row_count >= 1 via
          ``d.get("count") or d.get("total") or 1``, so an empty result
          set classifies OK — the exact ClinicalTrials/FDIC failure
          shape this module was written to catch ("HTTP 200 but ZERO
          results is a FAILURE, not a pass").

  RV11-B  family #1's cousin (component nothing calls): the derived-
          analysis loop's only production entry point is the finance
          plugin tool ``edgar_anomalies``, but no production entry
          registers the finance plugin — orchestrator._default_registry()
          seeds sports + compute ONLY. The loop is unreachable from the
          running system (same shape as run 10's Thompson routing /
          PredictionStore finding).

PINS (pass on current master; they lock behaviour this review verified
so it cannot drift silently):

  - gain-gate core rule under a seeded sweep: a genuinely fresh voice
    is never skipped while independence is unmet; a duplicate voice is
    always skipped when independence is the ONLY unmet requirement.
  - the cross-module string contract between
    agp.research_program.unmet_reasons and
    tools.pipeline.retrieval.estimate_gain ("independent sources <"):
    pinned loudly so wording drift changes THIS test, not gate
    semantics. (Verified drift consequence today: duplicate voices
    would then be skipped even when they could serve a non-independence
    shortfall — over-skipping, i.e. conservative, but silent.)
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from tests.helpers.no_socket import NoSocket  # noqa: E402

_nosocket = NoSocket()
_nosocket.install()

import random  # noqa: E402

from agp.research_program import (  # noqa: E402
    EvidenceRequirement,
    SourceClassRank,
)
from tools.pipeline.retrieval import estimate_gain  # noqa: E402
from tools.sources.base import SourceSpec  # noqa: E402


# ── RV11-A: zero results must be DEGRADED in the health probe ─────────────


def test_uspto_probe_zero_results_is_degraded_not_ok(monkeypatch):
    """Drive the REAL registered uspto probe thunk over a stub transport:
    HTTP-shaped success payload carrying total=0 (a zero-result known-good
    query). _finish exists precisely to call that DEGRADED."""
    import tools.sources.health as H

    class _FakeSrc:
        class spec:  # noqa: N801 - minimal attribute bag
            base_url = "https://api.uspto.gov/api/v1"

        def build_url(self, path, params):
            return "https://api.uspto.gov/api/v1" + path

    class _FakeAd:
        def search_applications(self, query, offset=0, limit=25):
            # The defect shape: reachable endpoint, ZERO hits, valid JSON.
            return {"total": 0, "patentFileDateDataBag": []}

    monkeypatch.setattr(H, "_build", lambda name: (_FakeSrc(), _FakeAd()))
    res = H.PROBES["uspto_odp"][1]()
    assert res.verdict == H.DEGRADED, (
        f"zero-result payload classified {res.verdict} with "
        f"row_count={res.row_count}: absence read as success"
    )


def test_health_count_helpers_cannot_map_zero_to_positive():
    """Any count_of used by _finish must preserve zero. The uspto probe's
    ``or 1`` fallback manufactures a positive count from absence."""
    import inspect

    import tools.sources.health as H

    src = inspect.getsource(H)
    assert 'or d.get("total") or 1' not in src and "or 1)" not in src.split(
        "def main")[0].split("PROBES:")[1], (
        "health.py contains an `or 1` count fallback: a missing/zero count "
        "becomes a positive row count and DEGRADED becomes unreachable")


# ── RV11-B: the derived-analysis loop must be reachable in production ─────


def test_edgar_anomalies_reachable_from_production_registry():
    """orchestrator._default_registry() IS the process toolkit. If the
    finance plugin is not registered there, edgar_anomalies (and the whole
    derived-analysis loop) is dead code regardless of its unit tests."""
    import orchestrator

    reg = orchestrator._default_registry()
    names = reg.tool_names_for(None, "edgar anomalies AAPL derived analysis")
    assert "edgar_anomalies" in names, (
        f"finance plugin absent from the production registry "
        f"({sorted(p.name for p in reg.plugins())}); the derived-analysis "
        f"loop has no caller in the running system"
    )


# ── PINS: expected-information-gain gate core rule (verified this run) ────


def _spec(name, base_url):
    return SourceSpec(name=name, base_url=base_url, description="",
                      answers=("generic scholarly works about things",),
                      tier=1, min_interval_s=0.0)


def test_sweep_fresh_voice_never_skipped_while_independence_unmet():
    rng = random.Random(20260823)
    for _ in range(300):
        n_req = rng.randint(1, 4)
        have = rng.randint(0, n_req - 1)
        reqs = EvidenceRequirement(min_independent_sources=n_req)
        keys = {f"fam{i}" for i in range(have)}
        est = estimate_gain(_spec(f"s{rng.random():09f}",
                                  f"https://h{rng.random():09f}.example"),
                            reqs, keys, "scholarly literature")
        assert est.worth_the_call, (
            "gate skipped a genuinely fresh voice while independence was "
            f"unmet (have={have}, need={n_req}) — premature-stop hazard"
        )


def test_sweep_duplicate_voice_skipped_on_pure_independence_shortfall():
    from tools.pipeline.retrieval import independence_key

    rng = random.Random(4711)
    for _ in range(200):
        n_req = rng.randint(2, 4)
        reqs = EvidenceRequirement(min_source_class=SourceClassRank.SECONDARY,
                                   min_independent_sources=n_req)
        spec = _spec("same_member", "https://same.example")
        keys = {independence_key("same_member", "https://same.example")}
        assert len(keys) < n_req
        est = estimate_gain(spec, reqs, keys, "scholarly literature")
        assert not est.worth_the_call, (
            "duplicate voice kept although independence was the only unmet "
            "requirement — the call cannot change the leaf"
        )


def test_pin_unmet_reason_wording_contract_with_gain_gate():
    """estimate_gain matches 'independent sources <' INSIDE the
    human-readable reason strings of EvidenceRequirement.unmet_reasons.
    Pin the exact substring so a rewording fails HERE, loudly, instead of
    silently changing which fetches the gate skips."""
    reqs = EvidenceRequirement(min_independent_sources=3)
    reasons = reqs.unmet_reasons(SourceClassRank.SECONDARY, 1,
                                 produced_quant=True)
    assert reasons, "shortfall must produce reasons"
    assert any("independent sources <" in r for r in reasons), (
        "unmet_reasons wording drifted out of contract with "
        "retrieval.estimate_gain's indep_short match"
    )
