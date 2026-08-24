"""Review run 3 (2026-08-24, ox-alpha reviewer) — reproductions.

All tests here FAIL on origin/master 96e09c9 by construction: each pins a
defect documented in findings/review_2026-08-24_run3.md. When a fix lands,
flip the assertion to make it a fix-pin.

Families per PATTERNS.md:
  R1/R2  family 1+3 — verification whose input can be missing / absence as
         success at the RestSource -> ledger seam
  R3     family 1+3 — provenance_is_intact is CIRCULAR on resume: it mints
         the bytes into the ledger it then verifies against
  R4     family 3   — cmefedfut adapter turns a 502 error body into
         settlements=[] with PRIMARY _fetch provenance
  R5     family 6   — engine seam rounds the SEALED confidence half-UP,
         contradicting floor_conf / sealable()
  R6     family 6   — inherited_ceiling round(,4) crosses TIER boundaries
  R7     M3a       — portfolio Kelly pays 2x for two copies of one bet
  R8     families 4+5 — relevance gate admits an empty result set that
         echoes the question text back (coverage 80%)
"""

import hashlib

import pytest


# ── R1: RestSource mints PRIMARY for any status, incl. 502 error bodies ──
def test_r1_error_body_from_non_200_never_mints_primary():
    from tools.sources.base import RestSource, SourceSpec

    class SpyLedger:
        def __init__(self):
            self.minted = []

        def record_tool_result(self, tool, body, primary=True, urls=None):
            self.minted.append((tool, primary))

    spec = SourceSpec(name="fake", base_url="https://x.example",
                      description="d", answers=["a"], cannot_answer=["c"],
                      tier=1, min_interval_s=0)

    def transport(url, headers):
        return 502, '{"error": "upstream exploded"}'

    rs = RestSource(spec, ledger=SpyLedger(), transport=transport)
    rs.get("https://x.example/q")
    # DEFECT: the 502's error body is already in the ledger, primary=True,
    # before any consumer sees the status code.
    assert not rs.ledger.minted, (
        f"non-200 error body minted PRIMARY: {rs.ledger.minted}")


def test_r2_get_json_returns_parsed_error_body_without_status_check():
    from tools.sources.base import RestSource, SourceSpec

    spec = SourceSpec(name="fake", base_url="https://x.example",
                      description="d", answers=["a"], cannot_answer=["c"],
                      tier=1, min_interval_s=0)

    def transport(url, headers):
        return 503, '{"error": "service unavailable"}'

    rs = RestSource(spec, ledger=None, transport=transport)
    # DEFECT: get_json hands back the parsed error body as if it were data;
    # nothing in this path consults rec.status.
    with pytest.raises(Exception):
        rs.get_json("https://x.example/q")


# ── R3: resumed-run integrity check is circular ──
def test_r3_provenance_is_intact_cannot_fail_for_self_consistent_bytes():
    from tools.pipeline.checkpoint import Checkpoint, provenance_is_intact

    class RealLedger:
        """Mimics ProvenanceLedger: stores what it is told, answers has_observation."""

        def __init__(self):
            self._bodies = set()

        def record_tool_result(self, tool, body, primary=True, urls=None):
            self._bodies.add(body)

        def has_observation(self, b):
            return b in self._bodies

    body = "FABRICATED EVIDENCE BYTES"
    rec = {"body": body, "url": "https://evil.example/x",
           "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
           "source_name": "openalex", "primary": True}
    ck = Checkpoint(run="r1", stage="fetch_leaf", key="k1", input_hash="h",
                    payload={"fetches": [rec]})
    led = RealLedger()
    ok = provenance_is_intact(led, [ck])
    # DEFECT: intact==True because replay_ledger FIRST wrote the fabricated
    # bytes INTO led, then has_observation() found them. Any attacker who can
    # write a checkpoint file with self-consistent hashes passes the guard;
    # under the default unkeyed regime nothing authenticates the payload.
    assert not ok, (
        "provenance_is_intact passed for bytes that only entered the "
        "ledger via the replay itself — the check is circular")


# ── R4: cmefedfut adapter — 502 becomes empty success with PRIMARY provenance ──
def test_r4_cmefedfut_settlements_rejects_error_body_instead_of_empty_list():
    from tools.sources.base import RestSource, SourceSpec
    from tools.sources.cmefedfut import CmeFedFutAdapter

    spec = SourceSpec(name="cmefedfut", base_url="https://x.example",
                      description="d", answers=["a"], cannot_answer=["c"],
                      tier=3, min_interval_s=0)

    def transport(url, headers):
        return 502, '{"error": "gateway timeout"}'

    adapter = CmeFedFutAdapter(
        RestSource(spec, ledger=None, transport=transport))
    out = adapter.settlements("20250815")
    # DEFECT: settlements=[] + _fetch.sha256 of the ERROR body — downstream
    # sees an honest-looking null backed by "primary exchange records".
    assert not out["settlements"] == [] or out["_fetch"].get("sha256") is None, (
        f"error body became empty-success with provenance: {out}")


# ── R5: engine seam raises the sealed number (round-half-up) ──
def test_r5_engine_seam_rounding_never_raises_the_sealed_confidence():
    import math
    # tools/pipeline/engine.py:531 writes
    #   out.confidence = round(max(0.0, min(ec.estimate, ec.ceiling)), 2)
    # while agp.estimate.EstimateCeiling.sealable() floors. Sweep both:
    for i in range(100001):
        est = i / 100000
        for ceil in (0.54, 0.55):
            exact = min(est, ceil)
            engine_value = round(exact, 2)
            sealable_value = math.floor(exact * 100) / 100
            assert engine_value <= exact + 1e-12, (
                f"engine seam RAISED {exact} -> {engine_value} "
                f"(floor_conf would give {sealable_value}); "
                f"first violation at est={est}, ceiling={ceil}")


# ── R6: inherited_ceiling round(,4) promotes tiers ──
def test_r6_inherited_ceiling_rounding_never_crosses_a_tier_boundary():
    import math
    import random
    from datetime import date

    from tools.research_program import (
        N_FOR_VERIFIED,
        SPECULATIVE_CAP,
        ResolutionRecord,
        inherited_ceiling,
        summarize_track_record,
        tier_ceiling_from_score,
        wilson_lower_bound,
    )

    random.seed(3)
    for _ in range(2000):
        n = random.randint(5, 400)
        recs = [ResolutionRecord(question_id=f"q{i}",
                                 resolved_at=date(2026, 1, 1),
                                 outcome=("hit" if random.random() < 0.85
                                          else "miss"),
                                 best_source_class="PRIMARY")
                for i in range(n)]
        v = inherited_ceiling(recs)
        tr = summarize_track_record(recs)
        support = wilson_lower_bound(tr.n_hit,
                                     max(0, tr.n_resolved - tr.n_stale))
        calib = max(0.0, 1.0 - 2.0 * tr.brier)
        sf = min(1.0, math.log1p(n) / math.log1p(N_FOR_VERIFIED))
        base = SPECULATIVE_CAP + (1.0 - SPECULATIVE_CAP) * (
            (0.55 * support + 0.45 * calib) * (0.5 + 0.5 * sf))
        exact = base - 0.20 * tr.stale_fraction
        floored = math.floor(min(score_cap := min(exact, 0.90),
                                 0.90) * 10000) / 10000 \
            if False else math.floor(min(exact, 0.90) * 10000) / 10000
        assert tier_ceiling_from_score(v) == tier_ceiling_from_score(floored), (
            f"rounding promoted the tier: exact={exact}, rounded={v}, "
            f"tier {tier_ceiling_from_score(floored)} -> "
            f"{tier_ceiling_from_score(v)}")


# ── R7: portfolio Kelly pays twice for one bet ──
def test_r7_portfolio_kelly_treats_identical_bets_as_one_position():
    pytest.importorskip("numpy")
    from tools.kelly import kelly_portfolio

    bet = {"edge": 0.05, "odds": -110, "correlation_with_others": 1.0}
    single = kelly_portfolio([dict(bet)])[0]["final_fraction"]
    doubled = kelly_portfolio([dict(bet), dict(bet)])
    total = sum(x["final_fraction"] for x in doubled)
    # Docstring: "Perfectly correlated bets ... treated as one position."
    assert total <= single * 1.001, (
        f"two copies of ONE position paid {total:.4f} vs {single:.4f} "
        f"single ({total / single:.2f}x)")


# ── R8: relevance gate admits an echoing empty result set ──
def test_r8_gate_does_not_admit_an_empty_result_set_echoing_the_question():
    from tools.pipeline.retrieval import RelevanceGate

    g = RelevanceGate()
    q = ("what does recent scholarly research say about semiconductor "
         "supply chain resilience")
    admitted, cov, _reason = g.judge(q, "descriptive", {"query": q,
                                                        "results": []})
    # DEFECT: coverage 0.80 — zero results, yet admissible evidence because
    # the page echoed the question text back. Volume of token overlap is
    # standing in for content (families 4/5).
    assert not admitted, (
        f"empty echo result admitted at coverage {cov:.0%}")
