"""PERF — CachingModel through the REAL pipeline (tests/test_perf_calls_e2e.py).

Integration pins for the call-removal lever:

1. IDENTICAL ANSWERS. Running the same question twice through
   ResearchPipeline with a CachingModel in front must produce byte-identical
   observable output (same golden fingerprint fields), with the second run
   served almost entirely from cache — decompose + leaf answers are pure
   functions of their prompts.
2. THE ADVERSARY STILL RUNS. The second run's attack is a fresh call even
   though its prompt is byte-identical — NON_CACHEABLE_ROLES guarantees a
   critic never inherits a stored verdict.
3. CUTOFF SAFETY IS STRUCTURAL. A run scoped to retro:2024-01-03 can never
   serve entries from retro:2024-05-22 (different key partition) — so future
   evidence cannot leak into a past-dated run through the cache. Pinned by
   demonstrating two differently-scoped models produce two independent real
   call streams on byte-identical prompts.

The engine itself is untouched (exclusive ownership); the wrapper sits at
the model seam exactly where production wires ProviderRouter today.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from tests.test_speed_parallel_leaves import (  # noqa: E402
    ROUTES,
    _Adversary,
    _answer,
    _decompose,
)
from tools.pipeline.cache import CachingModel, PromptCache  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402


def _run_cached(tmp_path, *, scope: str):
    """One pipeline run behind a scoped CachingModel; returns result + model."""
    inner = ScriptedModel({"Architect": [_decompose(3)]},
                          default=_answer(0.7))
    cache = PromptCache(str(tmp_path))
    model = CachingModel(inner, cache, scope=scope)

    from agp import Domain
    from agp.provenance import ProvenanceLedger
    from tools.artifacts import ArtifactStore
    from tools.pipeline.engine import ResearchPipeline, fixture_transport

    ledger = ProvenanceLedger()
    pipeline = ResearchPipeline(
        model=model, adversary_router=_Adversary(),
        transport=fixture_transport(dict(ROUTES)),
        store=ArtifactStore(root=tmp_path / "art"), ledger=ledger)
    result = asyncio.run(pipeline.run(
        "Will Apple report quarterly results above Wall Street consensus "
        "expectations in its next earnings report?",
        domain=Domain.FINANCIAL, today=date(2026, 8, 22)))
    return result, model


def _fingerprint(result) -> dict:
    return {
        "sealed": result.sealed,
        "refusal_reason": result.refusal_reason,
        "confidence_score": result.confidence_score,
        "conclusion": result.conclusion,
        "leaves": [{"text": l.text, "answer": l.answer,
                    "confidence": l.confidence, "tier": l.tier}
                   for l in result.leaves],
        "objections": [o.text for o in result.objections],
    }


def test_second_identical_run_is_cache_served_and_byte_identical(tmp_path):
    r1, m1 = _run_cached(tmp_path, scope="live:test")
    n_inner_calls_1 = m1.cache.misses
    r2, m2 = _run_cached(tmp_path, scope="live:test")

    assert _fingerprint(r1) == _fingerprint(r2)
    assert m2.cache.hits == n_inner_calls_1      # everything author-side hit
    assert m1.cache.hits == 0 and m1.cache.misses > 0


def test_adversary_runs_fresh_on_cache_hit_run(tmp_path):
    """Run 2 is fully cached author-side; the attack must still be a real
    fresh generation (hits==0 for Adversary role by construction — pinned
    via inner call count)."""
    calls = []

    class CountingAdv(_Adversary):
        async def complete(self, task_class, messages, schema=None):
            calls.append(1)
            return await super().complete(task_class, messages,
                                          schema=schema)

    adv = CountingAdv()

    def _run(scope_adv):
        inner = ScriptedModel({"Architect": [_decompose(3)]},
                              default=_answer(0.7))
        model = CachingModel(inner, PromptCache(str(tmp_path / scope_adv)),
                             scope="live:t")
        from agp import Domain
        from agp.provenance import ProvenanceLedger
        from tools.artifacts import ArtifactStore
        from tools.pipeline.engine import ResearchPipeline, \
            fixture_transport
        pipeline = ResearchPipeline(
            model=model, adversary_router=adv,
            transport=fixture_transport(dict(ROUTES)),
            store=ArtifactStore(root=tmp_path / f"art{scope_adv}"),
            ledger=ProvenanceLedger())
        return asyncio.run(pipeline.run(
            "Will Apple report quarterly results above Wall Street consensus "
            "expectations in its next earnings report?",
            domain=Domain.FINANCIAL, today=date(2026, 8, 22)))

    _run("a")
    _run("a")
    assert len(calls) == 2, "adversary must be called once per run, always"


def test_different_scopes_never_share_entries_even_byte_identical(tmp_path):
    """The cutoff guarantee, demonstrated end-to-end: identical prompt, two
    scopes -> two independent real generations. No entry crosses."""
    r_early, m_early = _run_cached(tmp_path, scope="retro:2024-01-03")
    r_late, m_late = _run_cached(tmp_path, scope="retro:2024-05-22")
    # Both runs did REAL work (no cross-scope service):
    assert m_early.cache.hits == 0
    assert m_late.cache.hits == 0
    # And both produced honest results independently:
    assert r_early.sealed == r_late.sealed  # same scripted world, fine —
    # what matters is neither was served from the other's partition.


def test_scoped_caching_model_rejects_live_mode_conflict(tmp_path):
    inner = ScriptedModel()
    with pytest.raises(ValueError):
        CachingModel(inner, PromptCache(str(tmp_path)),
                     scope="retro:2024-01-03", mode="live")


def test_no_scope_at_all_refuses_to_construct(tmp_path):
    with pytest.raises(ValueError, match="scope"):
        CachingModel(ScriptedModel(), PromptCache(str(tmp_path)))
