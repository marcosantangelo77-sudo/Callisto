"""
tests/test_hypgen_slice2.py — tests for the tools.hypgen.generation slice.

Covers:
  * analyze_cluster (hit rates, expected rates, feature modes, small clusters)
  * generate_from_templates via a fake HypothesisManager
    (temporal metadata, dedup, max_hypotheses cap, min_edge conversion,
     consensus_min_books coercion, per-template failure isolation)
  * generate_from_clusters via a fake vector store + fake manager
    (min_cluster_size gate, delta threshold, Over/Under side selection)
  * generate_from_ladder with a stubbed escalate_with_ladder
    (error path, unparseable content, happy-path persistence)
  * facade wiring: HypothesisGenerator methods delegate to the impls and
    keep historical attributes importable

Write-safety: none of these tests touch live-betting status paths; the
generator only ever creates drafts through create_hypothesis.
"""

import json

import pytest

import tools.hypothesis_generator as hg_mod
from tools.hypgen.generation import (
    analyze_cluster,
    generate_from_clusters,
    generate_from_ladder,
    generate_from_templates,
)


# ──────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────

class FakeManager:
    """Records create_hypothesis calls; returns sequential ids."""

    def __init__(self, existing_names=None, fail_on_name_substring=None):
        self.created = []
        self._names = list(existing_names or [])
        self.fail_on = fail_on_name_substring

    async def get_all_names(self):
        return list(self._names)

    async def create_hypothesis(self, *, name, thesis, sport, market_type,
                                model_config, edge_threshold, notes=""):
        if self.fail_on and self.fail_on in name:
            raise RuntimeError(f"injected failure for {name}")
        hid = f"hyp-{len(self.created) + 1}"
        self.created.append({
            "hypothesis_id": hid, "name": name, "thesis": thesis,
            "sport": sport, "market_type": market_type,
            "model_config": dict(model_config),
            "edge_threshold": edge_threshold, "notes": notes,
        })
        self._names.append(name)
        return hid


class FakeVectorStore:
    def __init__(self, clusters):
        self.clusters = clusters
        self.calls = []

    async def cluster_by_similarity(self, collection, threshold=0.85,
                                    data_period=None):
        self.calls.append({
            "collection": collection, "threshold": threshold,
            "data_period": data_period,
        })
        return self.clusters


def make_item(hit=True, edge=0.03, sport="basketball_nba",
              market="player_points", player=None, implied=0.5):
    meta = {"hit": hit, "edge": edge, "sport": sport, "market": market}
    if player is not None:
        meta["player"] = player
    meta["book_implied_over"] = implied
    return {"metadata": meta}


def cluster_of(n, **kw):
    return [make_item(**kw) for _ in range(n)]


# ──────────────────────────────────────────────────────────────
# analyze_cluster
# ──────────────────────────────────────────────────────────────

class TestAnalyzeCluster:
    def test_all_hits_full_rate(self):
        c = cluster_of(6, hit=True, edge=0.04)
        a = analyze_cluster(c)
        assert a["hit_rate"] == 1.0
        assert a["total_resolved"] == 6
        assert a["avg_edge"] == pytest.approx(0.04)

    def test_half_hits(self):
        items = cluster_of(3, hit=True) + cluster_of(3, hit=False)
        a = analyze_cluster(items)
        assert a["hit_rate"] == pytest.approx(0.5)

    def test_expected_rate_from_implied(self):
        items = ([make_item(implied=0.6)] * 3 +
                 [make_item(implied=0.4)] * 3)
        a = analyze_cluster(items)
        assert a["expected_rate"] == pytest.approx(0.5)

    def test_default_expected_when_missing(self):
        items = [{"metadata": {"hit": True}} for _ in range(5)]
        a = analyze_cluster(items)
        assert a["expected_rate"] == 0.5

    def test_too_few_resolved_returns_none(self):
        assert analyze_cluster(cluster_of(4)) is None

    def test_unresolved_items_do_not_count(self):
        items = cluster_of(5)
        items.append({"metadata": {}})
        a = analyze_cluster(items)
        assert a["total_resolved"] == 5

    def test_mode_features_and_pattern_desc(self):
        items = (
            cluster_of(4, sport="basketball_nba", market="player_points") +
            [make_item(sport="icehockey_nhl", market="player_assists")]
        )
        a = analyze_cluster(items)
        cf = a["common_features"]
        assert cf["sport"] == "basketball_nba"
        assert cf["market"] == "player_points"
        assert "nba" in cf["pattern_desc"]
        assert "points" in cf["pattern_desc"]

    def test_unique_players_counted(self):
        items = [
            make_item(player=p) for p in ["a", "b", "c", "d", "e"]
        ]
        a = analyze_cluster(items)
        assert a["common_features"]["unique_players"] == 5

    def test_mixed_pattern_desc_when_no_common(self):
        # no sport/market metadata at all → "mixed"
        items = [
            {"metadata": {"hit": bool(i % 2), "edge": 0.01}}
            for i in range(6)
        ]
        a = analyze_cluster(items)
        assert a["common_features"]["pattern_desc"].startswith("avg_edge")


# ──────────────────────────────────────────────────────────────
# generate_from_templates (via impl function directly)
# ──────────────────────────────────────────────────────────────

class _GenShim:
    """Minimal stand-in for HypothesisGenerator (manager only)."""

    def __init__(self, manager):
        self.hypothesis_manager = manager


class TestGenerateFromTemplates:
    @pytest.fixture()
    def templates_sport(self):
        from tools.hypgen.templates import HYPOTHESIS_TEMPLATES
        sports = [t["sport_filter"] for t in HYPOTHESIS_TEMPLATES]
        assert sports, "template corpus must not be empty"
        return HYPOTHESIS_TEMPLATES[0]["sport_filter"][0]

    @pytest.mark.asyncio
    async def test_creates_drafts_with_temporal_metadata(
            self, monkeypatch, templates_sport):
        from tools.hypgen import templates as tmod

        template = {
            "id": "t1",
            "name": "{team} over {line} points",
            "thesis": "{team} exceeds {line} points",
            "market_type": "player_points",
            "sport_filter": [templates_sport],
            "variables": {
                "team": ["Lakers", "Celtics"],
                "line": ["25"],
                "min_edge": [2],
            },
            "model_config": {
                "type": "consensus_devig",
                "consensus_min_books": "3",
                "note": "cfg-{team}",
            },
        }
        monkeypatch.setattr(tmod, "HYPOTHESIS_TEMPLATES", [template])
        import tools.hypgen.generation as gmod
        monkeypatch.setattr(gmod, "HYPOTHESIS_TEMPLATES", [template])

        mgr = FakeManager()
        created = await generate_from_templates(_GenShim(mgr), templates_sport)
        names = {c["name"] for c in created}
        assert names == {
            "Lakers over 25 points", "Celtics over 25 points",
        }
        for rec in mgr.created:
            mc = rec["model_config"]
            assert mc["training_period_start"]
            assert mc["training_period_end"]
            assert mc["forward_test_start"] > mc["training_period_end"]
            assert mc["consensus_min_books"] == 3  # coerced to int
            assert mc["note"] in ("cfg-Lakers", "cfg-Celtics")
            assert rec["edge_threshold"] == pytest.approx(0.02)
            assert rec["notes"].startswith("Auto-generated")

    @pytest.mark.asyncio
    async def test_skips_existing_names(self, monkeypatch, templates_sport):
        from tools.hypgen import templates as tmod
        import tools.hypgen.generation as gmod

        template = {
            "id": "t1",
            "name": "{team} over {line} points",
            "thesis": "x",
            "market_type": "m",
            "sport_filter": [templates_sport],
            "variables": {"team": ["A", "B"], "line": ["10"], "min_edge": [2]},
            "model_config": {},
        }
        monkeypatch.setattr(tmod, "HYPOTHESIS_TEMPLATES", [template])
        monkeypatch.setattr(gmod, "HYPOTHESIS_TEMPLATES", [template])

        mgr = FakeManager(existing_names=["A over 10 points"])
        created = await generate_from_templates(_GenShim(mgr), templates_sport)
        assert [c["name"] for c in created] == ["B over 10 points"]

    @pytest.mark.asyncio
    async def test_max_hypotheses_cap(self, monkeypatch, templates_sport):
        from tools.hypgen import templates as tmod
        import tools.hypgen.generation as gmod

        template = {
            "id": "t1",
            "name": "{n} combo",
            "thesis": "x",
            "market_type": "m",
            "sport_filter": [templates_sport],
            "variables": {"n": [str(i) for i in range(10)]},
            "model_config": {},
        }
        monkeypatch.setattr(tmod, "HYPOTHESIS_TEMPLATES", [template])
        monkeypatch.setattr(gmod, "HYPOTHESIS_TEMPLATES", [template])

        mgr = FakeManager()
        created = await generate_from_templates(
            _GenShim(mgr), templates_sport, max_hypotheses=3
        )
        assert len(created) == 3
        assert len(mgr.created) == 3

    @pytest.mark.asyncio
    async def test_per_combo_failure_isolated(
            self, monkeypatch, caplog, templates_sport):
        from tools.hypgen import templates as tmod
        import tools.hypgen.generation as gmod

        template = {
            "id": "t1",
            "name": "{team}",
            "thesis": "x",
            "market_type": "m",
            "sport_filter": [templates_sport],
            "variables": {"team": ["good1", "bad", "good2"], "line": ["1"],
                          "min_edge": [2]},
            "model_config": {},
        }
        monkeypatch.setattr(tmod, "HYPOTHESIS_TEMPLATES", [template])
        monkeypatch.setattr(gmod, "HYPOTHESIS_TEMPLATES", [template])

        mgr = FakeManager(fail_on_name_substring="bad")
        created = await generate_from_templates(_GenShim(mgr), templates_sport)
        assert [c["name"] for c in created] == ["good1", "good2"]

    @pytest.mark.asyncio
    async def test_training_cutoff_date_respected(self, monkeypatch,
                                                  templates_sport):
        from tools.hypgen import templates as tmod
        import tools.hypgen.generation as gmod

        template = {
            "id": "t1", "name": "{team}", "thesis": "x",
            "market_type": "m", "sport_filter": [templates_sport],
            "variables": {"team": ["A"], "line": ["1"], "min_edge": [2]},
            "model_config": {},
        }
        monkeypatch.setattr(tmod, "HYPOTHESIS_TEMPLATES", [template])
        monkeypatch.setattr(gmod, "HYPOTHESIS_TEMPLATES", [template])

        mgr = FakeManager()
        created = await generate_from_templates(
            _GenShim(mgr), templates_sport,
            training_cutoff_date="2025-06-01",
        )
        assert len(created) == 1
        mc = mgr.created[0]["model_config"]
        assert mc["training_period_end"] == "2025-06-01"
        assert mc["forward_test_start"] == "2025-06-02"


# ──────────────────────────────────────────────────────────────
# generate_from_clusters
# ──────────────────────────────────────────────────────────────

class TestGenerateFromClusters:
    @pytest.fixture()
    def shim(self):
        class S:
            hypothesis_manager = None
            vector_store = None
        return S()

    @pytest.mark.asyncio
    async def test_over_edge_cluster_creates_hypothesis(self, shim):
        hot = cluster_of(12, hit=True, implied=0.45)
        cold = cluster_of(12, hit=False, implied=0.55)
        shim.vector_store = FakeVectorStore([hot, cold])
        shim.hypothesis_manager = FakeManager()

        created = await generate_from_clusters(shim)
        assert len(created) == 2
        sides = []
        for rec in created:
            hyp = next(h for h in shim.hypothesis_manager.created
                       if h["hypothesis_id"] == rec["hypothesis_id"])
            sides.append("Over" if rec["delta"] > 0 else "Under")
            mc = hyp["model_config"]
            assert mc["type"] == "cluster_derived"
            assert mc["source_data_period"] == "all"
            assert mc["training_period_start"]
            assert abs(rec["delta"]) >= 0.05
            assert hyp["edge_threshold"] == pytest.approx(abs(rec["delta"]))
        assert "Over" in sides and "Under" in sides

    @pytest.mark.asyncio
    async def test_small_cluster_skipped(self, shim):
        shim.vector_store = FakeVectorStore([cluster_of(5, hit=True)])
        shim.hypothesis_manager = FakeManager()
        created = await generate_from_clusters(shim, min_cluster_size=10)
        assert created == []

    @pytest.mark.asyncio
    async def test_delta_below_threshold_skipped(self, shim):
        # hit rate 8/12 ≈ .667 vs implied .65 → tiny delta
        items = (cluster_of(8, hit=True, implied=0.65) +
                 cluster_of(4, hit=False, implied=0.65))
        shim.vector_store = FakeVectorStore([items])
        shim.hypothesis_manager = FakeManager()
        created = await generate_from_clusters(shim)
        assert created == []

    @pytest.mark.asyncio
    async def test_unanalyzable_cluster_skipped(self, shim):
        shim.vector_store = FakeVectorStore([
            [{"metadata": {}} for _ in range(15)]
        ])
        shim.hypothesis_manager = FakeManager()
        created = await generate_from_clusters(shim)
        assert created == []

    @pytest.mark.asyncio
    async def test_data_period_tagged_and_forwarded(self, shim):
        shim.vector_store = FakeVectorStore([cluster_of(12, hit=True)])
        shim.hypothesis_manager = FakeManager()
        created = await generate_from_clusters(shim, data_period="historical")
        assert created[0]["data_period"] == "historical"
        call = shim.vector_store.calls[0]
        assert call["data_period"] == "historical"

    @pytest.mark.asyncio
    async def test_persist_failure_does_not_raise(self, shim, caplog):
        hot = cluster_of(12, hit=True)
        shim.vector_store = FakeVectorStore([hot])
        shim.hypothesis_manager = FakeManager(
            fail_on_name_substring="Cluster-discovered")
        created = await generate_from_clusters(shim)
        assert created == []


# ──────────────────────────────────────────────────────────────
# generate_from_ladder
# ──────────────────────────────────────────────────────────────

def ladder_stub(result):
    calls = []

    async def fake(prompt, system_context="", task_type="", timeout=None, **kw):
        calls.append({"prompt": prompt, "system_context": system_context,
                      "task_type": task_type})
        return result

    return fake, calls


@pytest.fixture()
def patch_ladder(monkeypatch):
    def _patch(result):
        fake, calls = ladder_stub(result)
        import inference
        monkeypatch.setattr(inference, "escalate_with_ladder", fake)
        return calls
    return _patch


class TestGenerateFromLadder:
    @pytest.mark.asyncio
    async def test_error_result_returns_empty(self, patch_ladder):
        patch_ladder({"content": "", "error": "boom"})
        mgr = FakeManager()
        out = await generate_from_ladder(_GenShim(mgr), "nba", "summary")
        assert out == []
        assert mgr.created == []

    @pytest.mark.asyncio
    async def test_unparseable_content_returns_empty(self, patch_ladder):
        patch_ladder({"content": "no json here at all", "error": None})
        mgr = FakeManager()
        out = await generate_from_ladder(_GenShim(mgr), "nba", "summary")
        assert out == []

    @pytest.mark.asyncio
    async def test_happy_path_persists_with_temporal_metadata(
            self, patch_ladder):
        payload = [{
            "name": "Road dog ML",
            "thesis": "Home-favored dogs cover on rest advantage",
            "market_type": "moneylines",
            "edge_threshold": 0.03,
            "model_config": {"type": "custom"},
        }, {
            "name": "Default cfg",
            "thesis": "no model_config provided",
        }]
        calls = patch_ladder({
            "content": json.dumps(payload), "error": None,
            "model_used": "fake-model",
        })
        mgr = FakeManager()
        out = await generate_from_ladder(_GenShim(mgr), "nba", "summary")
        assert len(out) == 2
        assert all(o["source"] == "claude_code" for o in out)
        assert calls[0]["task_type"] == "hypothesis_gen"
        first = mgr.created[0]
        assert first["model_config"]["type"] == "custom"
        second = mgr.created[1]["model_config"]
        # default config injected when missing
        assert second["type"] == "consensus_devig"
        assert second["devig_method"] == "power"
        for rec in mgr.created:
            mc = rec["model_config"]
            assert mc["training_period_start"]
            assert mc["forward_test_start"] > mc["training_period_end"]
            assert rec["edge_threshold"] >= 0

    @pytest.mark.asyncio
    async def test_bad_edge_threshold_defaults(self, patch_ladder):
        payload = [{"name": "X", "thesis": "y", "edge_threshold": "not-a-number"}]
        patch_ladder({"content": json.dumps(payload), "error": None})
        mgr = FakeManager()
        # Per-candidate failures are logged and skipped, never raised.
        out = await generate_from_ladder(_GenShim(mgr), "nba", "s")
        assert out == []
        assert mgr.created == []


# ──────────────────────────────────────────────────────────────
# Facade delegation
# ──────────────────────────────────────────────────────────────

class TestFacadeDelegation:
    @pytest.mark.asyncio
    async def test_facade_routes_generate_from_templates(self, monkeypatch):
        seen = {}

        async def fake_impl(gen, sport, max_hypotheses=50,
                            training_cutoff_date=None):
            seen.update(sport=sport, max_hypotheses=max_hypotheses,
                        cutoff=training_cutoff_date, gen=gen)
            return [{"ok": True}]

        monkeypatch.setattr(
            hg_mod, "_generate_from_templates_impl", fake_impl)
        gen = hg_mod.HypothesisGenerator(FakeManager(), object())
        out = await gen.generate_from_templates("nba", max_hypotheses=7,
                                                training_cutoff_date="d")
        assert out == [{"ok": True}]
        assert seen["sport"] == "nba"
        assert seen["max_hypotheses"] == 7
        assert seen["cutoff"] == "d"
        assert isinstance(seen["gen"], hg_mod.HypothesisGenerator)

    @pytest.mark.asyncio
    async def test_facade_routes_generate_from_claude(self, monkeypatch):
        seen = {}

        async def fake_impl(gen, sport, summary):
            seen.update(sport=sport, summary=summary)
            return []

        monkeypatch.setattr(hg_mod, "_generate_from_ladder_impl", fake_impl)
        gen = hg_mod.HypothesisGenerator(FakeManager(), object())
        await gen.generate_from_claude("nhl", "data")
        assert seen == {"sport": "nhl", "summary": "data"}

    @pytest.mark.asyncio
    async def test_facade_routes_wiki_grounded(self, monkeypatch):
        seen = {}

        async def fake_impl(gen, sport, **kw):
            seen.update(sport=sport, kw=kw)
            return {"generated": []}

        monkeypatch.setattr(hg_mod, "_generate_wiki_grounded_impl", fake_impl)
        gen = hg_mod.HypothesisGenerator(FakeManager(), object())
        await gen.generate_wiki_grounded("nba", focus_market="spreads",
                                         n_candidates=4, max_keep=2)
        assert seen["kw"] == {"focus_market": "spreads", "n_candidates": 4,
                              "max_keep": 2, "include_seeds": True}

    def test_facade_analyze_cluster_delegates(self):
        gen = hg_mod.HypothesisGenerator(FakeManager(), object())
        assert gen._analyze_cluster(cluster_of(5)) is not None
        assert gen._analyze_cluster(cluster_of(2)) is None
        assert hg_mod._analyze_cluster_fn is analyze_cluster

    def test_facade_expand_variables(self):
        gen = hg_mod.HypothesisGenerator(FakeManager(), object())
        combos = gen._expand_variables({"a": [1, 2], "b": [3]})
        assert {frozenset(c.items()) for c in combos} == {
            frozenset({("a", 1), ("b", 3)}),
            frozenset({("a", 2), ("b", 3)}),
        }

    def test_historical_attributes_still_importable(self):
        assert hasattr(hg_mod, "HYPOTHESIS_TEMPLATES")
        assert hasattr(hg_mod, "NEGATIVE_EXAMPLES_N")
        assert hasattr(hg_mod, "WIKI_CONTEXT_TOP_K")
        assert hasattr(hg_mod, "PRIOR_CORPUS_SIM")
        assert hasattr(hg_mod, "CANDIDATE_DEDUP_SIM")
        assert hasattr(hg_mod, "DB_PATH")
        assert callable(hg_mod.parse_candidates)
        assert callable(hg_mod.enforce_variance)
        assert callable(hg_mod.pick_unexplored_seeds)
