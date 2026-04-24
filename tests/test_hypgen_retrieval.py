"""Retrieval-grounded hypothesis generator: prompt must include wiki context.

We monkey-patch:
  - ``tools.hypothesis_generator._retrieve_wiki_context`` (via subclass override)
    to return a known set of article dicts
  - ``tools.hypothesis_generator.escalate_with_ladder`` (via module injection)
    to capture the prompt and return a canned candidate list
  - ``tools.hypothesis_generator.embed_batch`` to return deterministic vectors

Then we assert every wiki article topic/title appears inside the prompt the
generator built.
"""

from __future__ import annotations

import json
import types
import pytest
import pytest_asyncio

from tools import hypothesis_generator as hg_mod
from tools.hypothesis_generator import HypothesisGenerator


FAKE_WIKI = [
    {
        "topic": "mlb_home_fav_day_drop",
        "title": "MLB home favorites drop 52% in day games",
        "summary": "Day MLB home favs underperform night games.",
        "content": "Detailed evidence body.",
        "domain": "SIGNAL", "confidence": 0.8,
        "similarity": 0.82,
    },
    {
        "topic": "mlb_umpire_zone_signal",
        "title": "Wide-zone umpires inflate K/9",
        "summary": "Umps with wide zones increase K-prop OVERs.",
        "content": "Evidence.",
        "domain": "SIGNAL", "confidence": 0.7,
        "similarity": 0.78,
    },
    {
        "topic": "mlb_pitcher_rest_signal",
        "title": "Pitcher rest saturation effect",
        "summary": "Starter performance caps after 6 days rest.",
        "content": "Evidence.",
        "domain": "SIGNAL", "confidence": 0.65,
        "similarity": 0.71,
    },
]


FAKE_CANDIDATES = [
    {
        "name": f"candidate_{i}",
        "market": "totals",
        "direction": "under",
        "cohort_filter": "game_contexts.home_team = 'COL'",
        "signal_logic": f"specific logic {i}",
        "min_signals": 250,
        "significance_level": 0.05,
        "stat_test": "binomial",
        "ic_prior_estimate": 0.02 + i * 0.001,
        "variance_justification": f"unique axis {i}",
        "thesis_statement": (
            f"MLB game totals at Coors Field with wind 15+ mph blowing in "
            f"cover Under at a 55% rate across n>=250 games (axis {i}), "
            f"versus 50% implied. Expected edge 3% on DraftKings, tested via "
            f"one-sided binomial at p<=0.05."
        ),
        "edge_threshold": 0.03,
        "model_config": {"devig_method": "power", "target_book": "draftkings",
                         "consensus_min_books": 3,
                         "context_factors": ["park_factor", "wind_speed"]},
    }
    for i in range(5)
]


class _CaptureHypMgr:
    """Minimal stub for HypothesisManager used only for create_hypothesis
    + get_all_names — we don't want to actually write rows."""

    def __init__(self):
        self.created: list[dict] = []
        self.db_path = ":memory:"

    async def initialize(self):
        pass

    async def close(self):
        pass

    async def get_all_names(self):
        return set()

    async def create_hypothesis(self, *, name, thesis, sport, market_type,
                                model_config, edge_threshold, **_kw):
        hid = f"h{len(self.created)}"
        self.created.append({
            "hypothesis_id": hid, "name": name, "thesis": thesis,
            "sport": sport, "market_type": market_type,
            "model_config": model_config, "edge_threshold": edge_threshold,
        })
        return hid


@pytest.fixture
def captured_prompt():
    return {}


@pytest.fixture(autouse=True)
def patch_ladder_and_embed(monkeypatch, captured_prompt):
    """Replace escalate_with_ladder + embed_batch with controllable fakes."""
    import inference

    async def fake_ladder(prompt, system_context="", task_type="", timeout=None,
                         **kw):
        captured_prompt["prompt"] = prompt
        captured_prompt["task_type"] = task_type
        return {
            "content": json.dumps(FAKE_CANDIDATES),
            "model_used": "fake-model",
            "quality": "medium",
            "ladder_step": 0,
        }

    monkeypatch.setattr(inference, "escalate_with_ladder", fake_ladder)

    async def fake_embed_batch(texts, batch_size=32):
        # Encode text index as a distinct basis vector so candidates
        # end up mutually orthogonal (low similarity).
        out = []
        for i, t in enumerate(texts):
            v = [0.0] * 16
            v[i % 16] = 1.0
            out.append(v)
        return out

    monkeypatch.setattr(hg_mod, "embed_batch", fake_embed_batch)
    yield


@pytest_asyncio.fixture
async def gen(monkeypatch):
    """Generator instance with wiki retrieval stubbed out to return FAKE_WIKI."""
    mgr = _CaptureHypMgr()
    # VectorStore unused in this path, but required by __init__.

    class _VS:
        async def initialize(self):
            pass

        async def close(self):
            pass

    g = HypothesisGenerator(mgr, _VS(), db_path=":memory:")

    async def _fake_retrieve(sport, focus_market):
        return list(FAKE_WIKI)

    async def _fake_rej(sport, focus_market, limit):
        return [{"name": "prior_rejected", "thesis": "tired filler", "notes": ""}]

    async def _fake_recent(sport, limit=50):
        return []

    async def _noop_init():
        return None

    g.initialize = _noop_init  # type: ignore
    g._retrieve_wiki_context = _fake_retrieve  # type: ignore
    g._retrieve_rejection_examples = _fake_rej  # type: ignore
    g._recent_theses = _fake_recent  # type: ignore
    # Replace _db to avoid any accidental DB touch.
    g._db = types.SimpleNamespace(execute=lambda *a, **k: None,
                                  commit=lambda: None)
    return g


@pytest.mark.asyncio
async def test_prompt_contains_all_wiki_articles(gen, captured_prompt):
    """All 3 fake wiki articles must be present in the generator's prompt."""
    res = await gen.generate_wiki_grounded(
        sport="baseball_mlb", focus_market="totals",
        n_candidates=5, max_keep=5, include_seeds=False,
    )
    prompt = captured_prompt.get("prompt", "")
    assert prompt, "prompt was not captured"

    for art in FAKE_WIKI:
        assert art["topic"] in prompt, (
            f"wiki topic {art['topic']} not in prompt"
        )
        assert art["title"] in prompt, (
            f"wiki title {art['title']} not in prompt"
        )

    # And the generator must have produced candidates + dispatched the
    # hypothesis_gen task_type through the ladder.
    assert captured_prompt["task_type"] == "hypothesis_gen"
    assert res["generated"], "expected at least one survivor candidate"


@pytest.mark.asyncio
async def test_prompt_contains_rejection_examples(gen, captured_prompt):
    await gen.generate_wiki_grounded(
        sport="baseball_mlb", focus_market="totals",
        n_candidates=5, max_keep=5, include_seeds=False,
    )
    prompt = captured_prompt["prompt"]
    assert "prior_rejected" in prompt
    assert "REJECTED" in prompt  # the negative-example header is present
