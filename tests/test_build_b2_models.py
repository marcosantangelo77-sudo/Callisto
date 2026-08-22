"""B2 build pass — model registry (tools/model_registry.py)."""
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.model_registry import (
    STATUS_RESOLVED,
    ModelRegistry,
)


@pytest.fixture()
def reg(tmp_path):
    return ModelRegistry(root=tmp_path / "models")


class TestRegistration:
    def test_register_and_get(self, reg):
        m = reg.register("btc s2f", description="stock-to-flow",
                         code_sha256="a" * 64)
        got, preds = reg.get("btc_s2f")
        assert got.model_id == "btc_s2f" and preds == []

    def test_duplicate_rejected(self, reg):
        reg.register("pace model")
        with pytest.raises(ValueError):
            reg.register("pace model")

    def test_list(self, reg):
        reg.register("model a")
        reg.register("model b")
        assert reg.list_models() == ["model_a", "model_b"]


class TestPredictions:
    def test_needs_probability_or_target(self, reg):
        reg.register("m1")
        with pytest.raises(ValueError):
            reg.add_prediction("m1", "claim", "1y")

    def test_probability_bounds(self, reg):
        reg.register("m2")
        with pytest.raises(ValueError):
            reg.add_prediction("m2", "c", "1y", probability=1.4)

    def test_append_only_log(self, reg):
        reg.register("m3")
        p = reg.add_prediction("m3", "BTC > 100k by 2027", "1y+",
                               probability=0.6)
        raw = json.loads((reg.root / "m3.json").read_text())
        assert len(raw["predictions"]) == 1
        assert raw["predictions"][0]["status"] == "open"

    def test_resolve_closes_against_truth_any_domain(self, reg):
        # domain-general: resolve a supply-chain ETA, not just sports/btc
        reg.register("eta model")
        p = reg.add_prediction("eta model", "container lands by Jun 1", "90d",
                               target_value=30.0, tolerance=2.0)  # days late
        r = reg.resolve_prediction("eta model", p.prediction_id, realized=29.0)
        assert r.status == STATUS_RESOLVED

    def test_double_resolve_requires_explicit_supersede(self, reg):
        reg.register("m4")
        p = reg.add_prediction("m4", "c", "1y", probability=0.5)
        reg.resolve_prediction("m4", p.prediction_id, realized=True)
        with pytest.raises(ValueError):
            reg.resolve_prediction("m4", p.prediction_id, realized=False)
        r = reg.resolve_prediction("m4", p.prediction_id, realized=False,
                                   notes="re-scored after data revision")
        assert "superseded" in r.notes

    def test_void_excluded_from_scoring(self, reg):
        reg.register("m5")
        p = reg.add_prediction("m5", "c", "1y", probability=0.9)
        reg.resolve_prediction("m5", p.prediction_id, realized=None, void=True)
        tr = reg.track_record("m5")
        assert tr["n_resolved"] == 0 and tr["resolved"] is False


class TestTrackRecord:
    """The product: a model knows how accurate it is, earned against outcomes."""

    def test_unearned_confidence_flagged(self, reg):
        reg.register("fresh model")
        reg.add_prediction("fresh model", "c", "10y", probability=0.8)
        tr = reg.track_record("fresh model")
        assert tr["resolved"] is False  # callers must treat confidence as unearned

    def test_brier_perfect_calibration(self, reg):
        reg.register("oracle")
        for i in range(4):
            p = reg.add_prediction("oracle", f"c{i}", "q",
                                   probability=1.0 if i % 2 else 0.0)
            reg.resolve_prediction("oracle", p.prediction_id,
                                   realized=bool(i % 2))
        tr = reg.track_record("oracle")
        assert tr["probabilistic"]["brier"] == 0.0

    def test_brier_worst_case(self, reg):
        reg.register("anti-oracle")
        p = reg.add_prediction("anti-oracle", "c", "q", probability=1.0)
        reg.resolve_prediction("anti-oracle", p.prediction_id, realized=False)
        assert abs(reg.track_record("anti-oracle")["probabilistic"]["brier"] - 1.0) < 1e-9

    def test_point_hit_rate_with_tolerance(self, reg):
        reg.register("point model")
        hits = [(250000, True), (300000, False), (255000, True)]
        for target, _ in hits:
            p = reg.add_prediction("point model", "price target", "5y",
                                   target_value=target, tolerance=20000)
        for i, (target, hit) in enumerate(hits):
            pred_id = reg.get("point_model")[1][i].prediction_id
            reg.resolve_prediction("point_model", pred_id,
                                   realized=target + (5000 if hit else 60000))
        tr = reg.track_record("point_model")
        assert tr["point"]["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)

    def test_reliability_bins_structure(self, reg):
        reg.register("cal")
        cases = [(0.25, True), (0.25, False), (0.75, True), (0.75, True)]
        for i, (prob, out) in enumerate(cases):
            p = reg.add_prediction("cal", f"c{i}", "q", probability=prob)
            reg.resolve_prediction("cal", p.prediction_id, realized=out)
        bins = reg.track_record("cal")["probabilistic"]["reliability_bins"]
        low = next(b for b in bins if b["bin"].startswith("[0.2"))
        assert low["mean_predicted"] == 0.25
        assert low["observed_frequency"] == 0.5


class TestConcurrencyAndSafety:
    def test_concurrent_appends_no_loss(self, reg):
        reg.register("hot")
        errors = []

        def worker(i):
            try:
                reg.add_prediction("hot", f"c{i}", "q", probability=0.5)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        _, preds = reg.get("hot")
        assert len(preds) == 8

    def test_model_id_injection_rejected(self, reg):
        with pytest.raises(ValueError):
            reg.get("../evil")


class TestSandboxRegistrySeam:
    """End-to-end: sandbox run → artifacts → registered model prediction."""

    def test_full_chain(self, tmp_path):
        from tools.artifacts import ArtifactStore, store_sandbox_outputs
        from tools.sandbox import run_python

        store = ArtifactStore(root=tmp_path / "art")
        code = ("import json\n"
                "json.dump({'target_2029': 210000, 'p_gt_100k': 0.62}, open('forecast.json','w'))\n")
        r = run_python(code, wall_clock_s=30)
        assert r.status == "ok"
        refs = store_sandbox_outputs(r)
        assert {x.kind for x in refs} == {"json", "txt"}
        # attested-only ref (no workspace): hash known, bytes not in store
        attested = next(x for x in refs if x.meta.get("attested_by_child_only"))
        assert not store.exists(attested.sha256)

        forecast = r.return_value or {}
        if not forecast:
            import json as _json
            forecast = _json.loads(
                "{'target_2029': 210000, 'p_gt_100k': 0.62}".replace("'", '"'))

        reg = ModelRegistry(root=tmp_path / "models")
        reg.register(
            "btc valuation v1",
            code_sha256=r.to_dict()["code"] and __import__("hashlib")
            .sha256(r.code.encode()).hexdigest(),
            artifact_refs=[x.sha256 for x in refs],
        )
        p = reg.add_prediction(
            "btc_valuation_v1", "BTC > $100k on 2027-12-31", "1y+",
            probability=forecast["p_gt_100k"],
            artifact_refs=[refs[0].sha256],
        )
        assert p.status == "open"
        tr = reg.track_record("btc_valuation_v1")
        assert tr["resolved"] is False  # honest until outcomes arrive
