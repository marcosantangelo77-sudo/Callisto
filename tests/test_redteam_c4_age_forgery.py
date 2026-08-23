"""C4 — produced_at is attacker-writable, so evidence age is cosmetic.

The checkpoint docstring promises resumed runs stay "honest about evidence
age". produced_at is a plain, unauthenticated JSON field: rewriting it to now()
makes 40-day-old checkpointed evidence report age ~0, and by the same mechanism
keeps it permanently immune to gc() because its age resets on every touch.

Fix: the record is HMAC-signed under the harness key (the same secret W5 wired
for publication proofs). Under a KEYED regime an unsigned or re-dated record
has untrusted age, and untrusted age is treated as maximally old — fail-closed,
so forged freshness cannot buy either trust or gc immunity.
"""
import json
from datetime import datetime, timedelta

import pytest

from tools.pipeline.checkpoint import Checkpoint, FileCheckpointer

KEY = "harness-secret"


def _saved(tmp_path, monkeypatch, produced_at):
    monkeypatch.setenv("CALLISTO_CUTOFF_KEY", KEY)
    cp = FileCheckpointer(root=tmp_path)
    return cp, cp.save("run-1", "fetch_leaf", "ih", {"fetches": []},
                       produced_at=produced_at)


def test_saved_checkpoint_is_signed(tmp_path, monkeypatch):
    cp, ck = _saved(tmp_path, monkeypatch, datetime(2026, 7, 1))
    assert getattr(ck, "sig", ""), "checkpoint saved without a signature"
    assert ck.verify_signature(KEY)


def test_redating_the_json_invalidates_the_signature(tmp_path, monkeypatch):
    """The exact attack: rewrite produced_at to now()."""
    cp, ck = _saved(tmp_path, monkeypatch, datetime(2026, 7, 1))
    path = next(tmp_path.glob("*/*.json"))
    d = json.loads(path.read_text())
    d["produced_at"] = datetime(2026, 8, 23).isoformat()   # forged freshness
    path.write_text(json.dumps(d, sort_keys=True))

    forged = Checkpoint.from_dict(json.loads(path.read_text()))
    assert not forged.verify_signature(KEY), "re-dated record still verified"


def test_untrusted_age_is_treated_as_maximally_old(tmp_path, monkeypatch):
    """Forged freshness must not buy trust OR gc immunity."""
    cp, ck = _saved(tmp_path, monkeypatch, datetime(2026, 7, 1))
    path = next(tmp_path.glob("*/*.json"))
    d = json.loads(path.read_text())
    d["produced_at"] = datetime(2026, 8, 23).isoformat()
    path.write_text(json.dumps(d, sort_keys=True))

    forged = Checkpoint.from_dict(json.loads(path.read_text()))
    age = forged.trusted_age_seconds(now=datetime(2026, 8, 23), key=KEY)
    assert age == float("inf"), \
        f"forged produced_at reported age {age}; must be maximally old"


def test_genuine_record_reports_its_real_age(tmp_path, monkeypatch):
    """The fix must not make honest checkpoints look stale."""
    cp, ck = _saved(tmp_path, monkeypatch, datetime(2026, 8, 22))
    age = ck.trusted_age_seconds(now=datetime(2026, 8, 23), key=KEY)
    assert 86000 < age < 87000, f"honest record reported age {age}"


def test_forged_freshness_does_not_grant_gc_immunity(tmp_path, monkeypatch):
    """The second half of C4: a rewritten date must not evade collection."""
    monkeypatch.setenv("CALLISTO_CUTOFF_KEY", KEY)
    cp = FileCheckpointer(root=tmp_path)
    cp.save("run-1", "fetch_leaf", "ih", {"fetches": []},
            produced_at=datetime(2026, 1, 1))          # genuinely ancient
    path = next(tmp_path.glob("*/*.json"))
    d = json.loads(path.read_text())
    d["produced_at"] = datetime(2026, 8, 23).isoformat()   # forged freshness
    path.write_text(json.dumps(d, sort_keys=True))

    removed = cp.gc(now=datetime(2026, 8, 23), max_age_days=30.0)
    assert removed, "forged produced_at bought permanent gc immunity"


def test_honest_recent_checkpoint_survives_gc(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_CUTOFF_KEY", KEY)
    cp = FileCheckpointer(root=tmp_path)
    cp.save("run-1", "fetch_leaf", "ih", {"fetches": []},
            produced_at=datetime(2026, 8, 22))
    removed = cp.gc(now=datetime(2026, 8, 23), max_age_days=30.0)
    assert not removed, "an honest recent checkpoint was collected"
