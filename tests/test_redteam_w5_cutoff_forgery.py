"""W5 — an unkeyed CutoffEnforcer admits forged publication proofs.

The class docstring claims "all defaults are fail-closed". The signature check
in _reject_reason runs only `if self._signing_key`, and the default constructor
passes None — so on the DEFAULT path, which is the only path production uses
(tools/retrodiction/harness.py builds CutoffEnforcer with no key), any attacker
who controls the bytes can declare ANY publication date and be admitted.

That is the exact forgery the signature system exists to close; cutoff.py's own
comment says so: "Without a valid signature anyone could fabricate a proof
claiming any date over any bytes."

Consequence: retrodiction scores are meaningless. Post-cutoff evidence simply
declares a pre-cutoff date and walks in, so the model is scored on information
it could not have had.
"""
import hashlib
from datetime import date, datetime

import pytest

from tools.retrodiction.cutoff import (
    CutoffEnforcer, EvidenceRecord, PublicationProof, ProofKind,
)

CUTOFF = date(2024, 6, 1)
KEY = "harness-secret"


def _forged(content="post-cutoff scoop", claimed=date(1990, 1, 1)):
    """Attacker-controlled bytes, attacker-declared date, no signature."""
    return EvidenceRecord(
        url="https://evil/leak", query="q",
        fetched_at=datetime(2026, 8, 23, 12, 0), content=content,
        proof=PublicationProof(
            kind=ProofKind.SOURCE_DECLARED, published_on=claimed,
            locator="acc-forged",
            content_sha256=hashlib.sha256(content.encode()).hexdigest()))


def test_unkeyed_enforcer_rejects_unsigned_proof():
    """The default (unkeyed) enforcer must not admit an unsigned proof."""
    admitted, rejected = CutoffEnforcer(CUTOFF).admit([_forged()])
    assert not admitted, "forged proof admitted by the DEFAULT enforcer"
    assert rejected


def test_keyed_enforcer_still_admits_a_properly_signed_proof():
    """The fix must not break the legitimate signed path."""
    rec = _forged(claimed=date(2024, 3, 1))
    rec.proof = rec.proof.sign(KEY)
    admitted, rejected = CutoffEnforcer(CUTOFF, signing_key=KEY).admit([rec])
    assert len(admitted) == 1 and not rejected


def test_explicit_opt_in_is_required_to_run_unsigned():
    """Running without signatures must be a deliberate, visible act."""
    admitted, _ = CutoffEnforcer(CUTOFF, allow_unsigned=True).admit(
        [_forged(claimed=date(2024, 3, 1))])
    assert len(admitted) == 1, "explicit opt-in should still work"


def test_production_harness_does_not_run_unkeyed():
    """tools/retrodiction/harness.py must not build a fail-open enforcer."""
    src = open("tools/retrodiction/harness.py").read()
    assert "CutoffEnforcer(" in src
    idx = src.index("CutoffEnforcer(")
    call = src[idx:idx + 200]
    assert ("signing_key" in call) or ("allow_unsigned" in call), \
        "harness builds CutoffEnforcer with neither a key nor an explicit opt-in"
