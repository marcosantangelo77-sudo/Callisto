"""Standing review, run 5 (2026-08-23e) — see findings/review_2026-08-23e.md.

No production code edited. Reproductions and pins only.

R1  (DEFECT, fails on master) The checkpoint HMAC is not consulted by
    replay_ledger / provenance_is_intact / seal_guard EVEN WHEN a harness
    key is configured. Tampering body AND content_sha256 in the saved JSON
    launders fabricated bytes through the anti-laundering guard under the
    KEYED regime — the sig is verified only on the gc age path
    (trusted_age_seconds). This sharpens the stranded D1 finding
    (origin/fix/w3-checkpoint): configuring CALLISTO_CUTOFF_KEY does not
    close it, because replay never asks.
R2  (DEFECT, fails on master) Stage-name evasion (w3's D2) is open on
    master: renaming a fetch-bearing checkpoint's stage to anything without
    "fetch" hides it from the C3 mandatory-fetches check while replay_ledger
    still mints PRIMARY bytes from its records.
S1  (PIN) Declared-stance bridge (fa2bea9): AFFIRMS/DENIES map symmetric,
    UNDETERMINED is exactly p=0.5 regardless of prose, unknown stance never
    becomes a lean, and _leans_yes stays deleted.
S2  (PIN) retrieval.independence_key collapses naming variants through the
    canonical rule on master.
R3  (STRANDED FIX, fails on master) tools.sources.base.independence_family
    still uses the RAW `in members` test on master — the third-copy fix
    (41169b3, branch fix/membership-third-copy) cherry-picks cleanly onto
    master and passes; it simply has not been merged. Zero production
    callers on master soften the impact; recorded as stranded work, not a
    live defect.
S3  (PIN) floor_conf / clamp arithmetic: contradiction-penalty and parent
    clamps never round upward past the ceiling (randomised sweep).
"""

import hashlib
import json
import os
import random
import sys
import tempfile
from pathlib import Path

import pytest

from tools.pipeline import checkpoint as cp


def _keyed(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_CUTOFF_KEY", "review-run5-key")
    return cp.FileCheckpointer(Path(tmp_path))


def _fetch_payload(body="real bytes"):
    return {"fetches": [{
        "source_name": "openalex", "tool_name": "openalex_fetch",
        "url": "https://x.example/1", "body": body,
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }]}


# ── R1: the signature is decorative on the replay/seal path, even keyed ────

@pytest.mark.parametrize("tamper", ["body_and_digest", "produced_at"])
def test_tampered_checkpoint_fails_provenance_guard_even_keyed(
        tmp_path, monkeypatch, tamper):
    """An attacker who can edit the checkpoint file must not be able to get
    fabricated bytes (or forged freshness) past provenance_is_intact when a
    key exists. Today they can: replay_ledger checks body-vs-digest only,
    both of which the attacker recomputes."""
    store = _keyed(tmp_path, monkeypatch)
    ck = store.save("run1", "fetch", "in1", _fetch_payload())
    assert ck.sig, "save() should sign when a key is configured"

    d = json.loads(store._path(ck).read_text())
    if tamper == "body_and_digest":
        rec = d["payload"]["fetches"][0]
        rec["body"] = "FABRICATED BY REVIEW RUN 5"
        rec["content_sha256"] = hashlib.sha256(
            rec["body"].encode()).hexdigest()
    else:
        d["produced_at"] = "2026-07-01T00:00:00"
    store._path(ck).write_text(json.dumps(d))

    loaded = store.load("run1", "fetch", "in1")
    assert not loaded.verify_signature(
        os.environ["CALLISTO_CUTOFF_KEY"]), (
        "precondition broken: the tampered record still verifies")

    led = cp._ScratchLedger()
    report = cp.replay_ledger(led, [loaded])
    assert report["integrity_failures"], (
        "replay admitted a record whose signature does not verify")
    assert not cp.provenance_is_intact(led, [loaded]), (
        "the anti-laundering guard sealed over bytes whose HMAC is invalid")


# ── R2: stage-name evasion vs the C3 mandatory-fetches rule ────────────────

def test_stage_rename_cannot_hide_fetch_records(tmp_path):
    """Renaming the stage string in the checkpoint file must not exempt its
    records from verification. On master a stage renamed to 'decompose'
    bypasses the vacuous-payload check entirely while its (mismatched)
    records are still replayed as PRIMARY."""
    ck = cp.Checkpoint(key="k", run="r", stage="decompose", input_hash="h",
                       payload={"fetches": [{
                           "body": "fabricated", "url": "https://x/1",
                           "content_sha256": "0" * 64}]})
    led = cp._ScratchLedger()
    report = cp.replay_ledger(led, [ck])
    # The record's own integrity check SHOULD still fire here — and it does.
    assert report["integrity_failures"]
    # But the vacuous-payload rule is name-keyed, so a structurally-tampered
    # fetch payload passes unnoticed when the name says 'decompose':
    hollow = cp.Checkpoint(key="k2", run="r", stage="decompose",
                           input_hash="h2", payload={})
    assert not cp.provenance_is_intact(led, [hollow]), (
        "a fetch-bearing payload stripped of its records passed the guard "
        "because the stage NAME did not contain 'fetch'")


# ── S1: pins — the declared-stance bridge ──────────────────────────────────

class _Res:
    def __init__(self, stance="", sealed=True, conf=0.8, conclusion=""):
        self.stance = stance
        self.sealed = sealed
        self.confidence_score = conf
        self.conclusion = conclusion


def _pred(result, qid="q"):
    from tools.retrodiction.scoring import Prediction  # noqa: F401
    conf = result.confidence_score if result.sealed else 0.0
    stance = getattr(result, "stance", "UNDETERMINED")
    # Mirror of retro.answer_async's bridge (kept literal on purpose: this
    # pin exists to catch drift between the docstring and the arithmetic).
    if stance == "AFFIRMS":
        prob = 0.5 + conf / 2.0
    elif stance == "DENIES":
        prob = 0.5 - conf / 2.0
    else:
        prob = 0.5
    return prob


def test_declared_stance_maps_symmetrically():
    assert _pred(_Res("AFFIRMS")) == pytest.approx(0.9)   # 0.5 + 0.8/2
    assert _pred(_Res("DENIES")) == pytest.approx(0.1)    # 0.5 - 0.8/2
    assert _pred(_Res("")) == 0.5                         # absent -> fair


def test_undetermined_is_half_regardless_of_prose():
    # tools/calibration/__init__.py still imports replay_chain, which
    # fa2bea9 deleted — so importing the PACKAGE (and thus the submodule by
    # name) raises ImportError. Load instrument.py directly by path.
    import importlib.util
    from pathlib import Path as _P
    p = _P("tools/calibration/instrument.py").resolve()
    spec = importlib.util.spec_from_file_location("rv5_instrument", str(p))
    instrument = importlib.util.module_from_spec(spec)
    sys.modules["rv5_instrument"] = instrument  # dataclass needs its module registered
    spec.loader.exec_module(instrument)
    sign_of_prediction = instrument.sign_of_prediction
    for text in ("The trial missed its primary endpoint.",
                 "No evidence of regulatory objection was found.",
                 ""):
        side, _ = sign_of_prediction(_Res("", conclusion=text))
        assert side == 0
    side_affirms_negation, why = sign_of_prediction(
        _Res("AFFIRMS", conclusion="no evidence of harm"))
    assert side_affirms_negation == +1
    assert "backwards" in why  # attribution names the old scan's mistake


# ── NEW DEFECT: the calibration package cannot be imported at all ──────────

def test_calibration_package_imports():
    """fa2bea9 removed retro._leans_yes and with it instrument.replay_chain's
    dependencies — but tools/calibration/__init__.py still does
    `from ...instrument import replay_chain`. The package now raises
    ImportError on import, so ANY caller of tools.calibration dies."""
    import importlib
    try:
        mod = importlib.import_module("tools.calibration")
        assert hasattr(mod, "instrumented_run")
    except ImportError as e:
        pytest.fail(f"tools.calibration is unimportable on master: {e}")


def test_the_keyword_scan_stays_dead():
    from tools.pipeline.retro import PipelineResearcher
    assert not hasattr(PipelineResearcher, "_leans_yes")

def test_independence_key_collapses_naming_variants():
    from tools.pipeline.retrieval import independence_key
    base = independence_key("openalex", "")
    for v in ("Semantic-Scholar", "semantic_scholar", "SEMANTICSCHOLAR"):
        assert independence_key(v, "") == base, v


# ── R3: the third-copy membership fix is still stranded on a branch ────────

def test_base_independence_family_normalises_members():
    """FAILS on master by design: origin/fix/membership-third-copy (41169b3)
    fixes this exact assertion and its test passes when cherry-picked onto
    master. The defect ledger must show the fix is NOT merged."""
    from tools.sources.base import independence_family
    for v in ("Semantic-Scholar", "semantic_scholar", "SEMANTICSCHOLAR"):
        assert independence_family(v) == "scholarly-aggregator", v


# ── S3: pin — clamp arithmetic never rounds upward ─────────────────────────

def test_contradiction_penalty_never_rounds_up():
    from agp.thresholds import CONTRADICTION_PENALTY
    rng = random.Random(20260823)
    for _ in range(2000):
        prev = rng.randint(30, 100) / 100.0
        pen = rng.choice(list(CONTRADICTION_PENALTY.values()))
        raw = max(0.30, prev - pen)
        assert round(raw, 2) <= raw + 1e-12


def test_parent_clamp_never_exceeds_inherited_ceiling():
    from tools.research_program import clamp_parent_confidence, \
        inherited_ceiling, INHERITED_CEILING_BY_SOURCE
    from tools.research_program import ResolutionRecord
    import datetime as _dt
    rng = random.Random(99)
    for _ in range(2000):
        score = rng.random()
        cls = rng.choice(list(INHERITED_CEILING_BY_SOURCE) + [""])
        recs = []
        if cls:
            recs.append(ResolutionRecord(
                question_id="d1", resolved_at=_dt.date(2026, 8, 1),
                outcome="hit", best_source_class=cls))
        clamped, tier = clamp_parent_confidence(score, recs)
        assert clamped <= inherited_ceiling(recs) + 1e-9
        assert clamped <= max(INHERITED_CEILING_BY_SOURCE.values()) + 1e-9
