"""Variance-enforced dedup in the wiki-grounded generator.

Strategy:
  - 8 synthetic candidates; pair (0, 1) have *identical* embedding so their
    cosine sim = 1.0 ≥ 0.85 threshold and the lower-scored one must be dropped
  - candidates 2..7 each get an orthogonal basis vector, keeping them all

Expected survivors: 7.
"""

from __future__ import annotations

import json
import types
import pytest
import pytest_asyncio

from tools import hypothesis_generator as hg_mod
from tools.hypothesis_generator import HypothesisGenerator


N = 8

CANDIDATES = [
    {
        "name": f"c{i}",
        "market": "totals",
        "direction": "under",
        "cohort_filter": f"game_contexts.x = {i}",
        "signal_logic": f"logic {i}",
        "min_signals": 250,
        "significance_level": 0.05,
        "stat_test": "binomial",
        "ic_prior_estimate": 0.02 + i * 0.001,  # c7 highest → survives duplicate race
        "variance_justification": f"axis {i}",
        "thesis_statement": (
            f"MLB game totals at Coors Field with wind 15+ mph blowing in "
            f"cover Under at a 55% rate across n>=250 games (axis {i}), "
            f"versus a 50% implied baseline. Expected edge is 3% on DraftKings, "
            f"tested via one-sided binomial at p<=0.05."
        ),
        "edge_threshold": 0.03,
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["wind_speed", "park_factor"],
        },
    }
    for i in range(N)
]


def _basis(i: int, dim: int = 16) -> list[float]:
    v = [0.0] * dim
    v[i % dim] = 1.0
    return v


# Inject near-duplicate: candidate 1 shares candidate 0's vector exactly.
# cosine_similarity = 1.0 → ≥ CANDIDATE_DEDUP_SIM.
FAKE_EMBS = [_basis(0)] + [_basis(0)] + [_basis(i) for i in range(2, N)]


class _Stub:
    """Stub hypothesis manager."""

    def __init__(self):
        self.created = []
        self.db_path = ":memory:"

    async def get_all_names(self):
        return set()

    async def create_hypothesis(self, *, name, **_kw):
        self.created.append(name)
        return f"h{len(self.created)}"


@pytest.fixture(autouse=True)
def patch_ladder_and_embed(monkeypatch):
    import inference

    async def fake_ladder(prompt, system_context="", task_type="", timeout=None,
                         **kw):
        return {"content": json.dumps(CANDIDATES),
                "model_used": "fake", "quality": "med", "ladder_step": 0}

    monkeypatch.setattr(inference, "escalate_with_ladder", fake_ladder)

    async def fake_embed_batch(texts, batch_size=32):
        # Candidate thesis embeddings first N items; wiki embeddings after.
        # We feed back FAKE_EMBS plus unique vectors for any additional
        # (wiki-summary) calls.
        if len(texts) == N:
            return list(FAKE_EMBS)
        # wiki summary path — unique orthogonal vectors starting after N.
        return [_basis(100 + i, dim=128) for i in range(len(texts))]

    monkeypatch.setattr(hg_mod, "embed_batch", fake_embed_batch)


@pytest_asyncio.fixture
async def gen():
    mgr = _Stub()

    class _VS:
        async def initialize(self):
            pass

        async def close(self):
            pass

    g = HypothesisGenerator(mgr, _VS(), db_path=":memory:")

    async def _no_wiki(sport, focus_market):
        return []

    async def _no_rej(sport, focus_market, limit):
        return []

    async def _no_recent(sport, limit=50):
        return []

    async def _noop_init():
        return None

    g.initialize = _noop_init  # type: ignore
    g._retrieve_wiki_context = _no_wiki  # type: ignore
    g._retrieve_rejection_examples = _no_rej  # type: ignore
    g._recent_theses = _no_recent  # type: ignore
    g._db = types.SimpleNamespace(execute=lambda *a, **k: None,
                                  commit=lambda: None)
    return g


@pytest.mark.asyncio
async def test_dedup_drops_one_of_two_near_duplicates(gen):
    """Expected: candidate 0 (sim=1.0 with c1, lower ic_prior) is dropped;
    7 survive."""
    res = await gen.generate_wiki_grounded(
        sport="baseball_mlb", focus_market="totals",
        n_candidates=N, max_keep=N, include_seeds=False,
    )
    assert len(res["generated"]) == N - 1, (
        f"expected {N - 1} survivors after dedup, got {len(res['generated'])}"
    )
    # The dropped one must be c0 (lower ic_prior_estimate than c1).
    kept_names = {g["name"] for g in res["generated"]}
    assert "c0" not in kept_names, "c0 should have been dropped vs c1 (higher ic)"
    assert "c1" in kept_names
    # Diversity metric must be positive (kept items are orthogonal).
    assert res["diversity_metric"] > 0.3, (
        f"expected diverse survivors, got {res['diversity_metric']}"
    )


@pytest.mark.asyncio
async def test_all_unique_no_drops(gen, monkeypatch):
    """If every candidate is orthogonal, nothing is dropped."""
    import inference

    unique_cands = [
        {**c, "name": f"u{i}"} for i, c in enumerate(CANDIDATES)
    ]

    async def fake_ladder(prompt, system_context="", task_type="", timeout=None, **kw):
        return {"content": json.dumps(unique_cands),
                "model_used": "fake", "quality": "med", "ladder_step": 0}

    monkeypatch.setattr(inference, "escalate_with_ladder", fake_ladder)

    async def fake_embed_batch(texts, batch_size=32):
        if len(texts) == N:
            return [_basis(i) for i in range(N)]
        return [_basis(100 + i, dim=128) for i in range(len(texts))]

    monkeypatch.setattr(hg_mod, "embed_batch", fake_embed_batch)

    res = await gen.generate_wiki_grounded(
        sport="baseball_mlb", focus_market="totals",
        n_candidates=N, max_keep=N, include_seeds=False,
    )
    assert len(res["generated"]) == N
