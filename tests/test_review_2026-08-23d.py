"""Standing review, run 4 (2026-08-23d) — see findings/review_2026-08-23d.md.

No production code edited. This file contains reproductions and pins only.

P1  — 3 of the merged laundering repros crash on NameError before asserting.
P2  — claims.recompute_confidence can round ONTO a ceiling but never PAST it
      (corrects run-3's R5: the tier system is not violated).
D1  — checkpoint body tampering + digest rehash launders fabricated bytes
      through replay_ledger/provenance_is_intact with no key configured
      (reproduces the stranded fix/w3-checkpoint finding against master).
"""

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from agp.claims import recompute_confidence
from agp.provenance import SourceClass


# ── P1: the open-defect ledger crashes instead of asserting ────────────────


def test_laundering_repros_execute():
    """Three TestInheritanceRule repros die on NameError before any assertion.

    The file imports clamp_parent_confidence but not inherited_ceiling from
    tools.research_program. A repro that cannot run records nothing; this is
    the same failure mode as R3's right-invariant/wrong-inputs test, one level
    up — the ledger LOOKS maintained while reporting nothing.
    """
    import tests.test_redteam_confidence_laundering as ledger

    for name in ("test_stale_resolutions_earn_hit_rate_credit",
                 "test_pinball_none_on_quantile_style_record_scores_as_clean_hit",
                 "test_best_source_class_is_self_reported_on_records"):
        fn = getattr(ledger.TestInheritanceRule, name)
        with pytest.raises(NameError, match="inherited_ceiling"):
            # bound method -> needs self; call via a bare instance of the class
            fn(ledger.TestInheritanceRule())


# ── P2: recompute_confidence rounds ONTO ceilings, never past them ─────────


@pytest.mark.parametrize("source_class", list(SourceClass))
def test_clamp_never_exceeds_ceiling(source_class):
    """Corrects run-3's R5: rounding lands at most ON the ceiling.

    The value is min/max-clamped to the ceiling table BEFORE round(), and all
    ceilings are already 2dp — so no input can round PAST its ceiling. What
    remains is within-ceiling uplift (0.7499 -> 0.75), a floor_conf style
    inconsistency, not a tier violation.
    """
    from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
    import random

    rng = random.Random(20260823)
    ceiling = MAX_CONFIDENCE_BY_SOURCE[source_class.value]
    ev = type("E", (), {"assigned_class": source_class})()
    for _ in range(2000):
        claimed = rng.uniform(0.0, 1.0)
        out = recompute_confidence([ev], claimed)
        assert out <= ceiling + 1e-9, (claimed, out, ceiling)


# ── D1 reproduction: unsigned checkpoints launder across resume ────────────


def test_checkpoint_body_tamper_with_digest_rehash_is_not_caught():
    """save() signs only when a key is configured; NOTHING verifies the sig.

    Tamper the stored JSON's body, recompute content_sha256 to match, reload,
    replay: zero integrity failures, provenance reported intact. On master
    verify_signature has no callers outside checkpoint.py itself.
    """
    import tools.pipeline.checkpoint as ckpt_mod
    from tools.pipeline.checkpoint import (
        FileCheckpointer, replay_ledger, provenance_is_intact)

    tmp = tempfile.mkdtemp()
    cp = FileCheckpointer(root=Path(tmp))
    body = "real bytes"
    payload = {"fetches": [{
        "body": body, "url": "https://example.test",
        "content_sha256": hashlib.sha256(body.encode()).hexdigest()}]}
    ck = cp.save("review-run", "fetch_leaf", "ih1", payload)

    # attacker edits bytes and rehashes the digest on disk
    path = next(Path(tmp).rglob("*.json"))
    d = json.loads(path.read_text())
    fake = "FABRICATED bytes"
    d["payload"]["fetches"][0]["body"] = fake
    d["payload"]["fetches"][0]["content_sha256"] = \
        hashlib.sha256(fake.encode()).hexdigest()
    path.write_text(json.dumps(d))

    loaded = cp.load_by_key("review-run", ck.key)
    assert not loaded.sig                      # unsigned: no key configured
    assert ckpt_mod._harness_key() is None     # and none is set here either

    class Sink:
        def record_tool_result(self, *a, **k):
            pass
        def has_observation(self, b):
            return True                        # engine replayed it in

    report = replay_ledger(Sink(), [loaded])
    assert report["integrity_failures"] == []  # fabricated bytes pass clean
    assert provenance_is_intact(Sink(), [loaded]) is True


# ── control: WITH a key and a verifying consumer, D1 is catchable ──────────


def test_control_signature_verification_catches_tampering():
    """Proves the D1 repro fails for the stated reason (missing verifier),
    not because signatures cannot detect this tampering."""
    import tools.pipeline.checkpoint as ckpt_mod
    from tools.pipeline.checkpoint import FileCheckpointer

    ckpt_mod.os.environ["CALLISTO_CUTOFF_KEY"] = "review-test-key"
    try:
        tmp = tempfile.mkdtemp()
        cp = FileCheckpointer(root=Path(tmp))
        payload = {"fetches": [{"body": "real",
                                "url": "u",
                                "content_sha256":
                                    hashlib.sha256(b"real").hexdigest()}]}
        ck = cp.save("run-ctl", "fetch_leaf", "ih", payload)

        path = next(Path(tmp).rglob("*.json"))
        d = json.loads(path.read_text())
        d["payload"]["fetches"][0]["body"] = "evil"
        path.write_text(json.dumps(d))

        loaded = cp.load_by_key("run-ctl", ck.key)
        assert loaded.sig is not None                       # now signed
        assert not loaded.verify_signature("review-test-key")  # and caught
    finally:
        del ckpt_mod.os.environ["CALLISTO_CUTOFF_KEY"]
