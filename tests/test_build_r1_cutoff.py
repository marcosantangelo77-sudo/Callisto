"""Cutoff mechanism tests: every test here tries to LEAK and must fail to.

No network. Fixtures only.

NOTE ON allow_unsigned=True: these tests exercise the DATE and BYTES policies.
Signature policy is fail-closed by default (W5), so without the explicit opt-in
every record here would be rejected as "unkeyed regime" before its actual
subject was reached — and the rejection assertions would pass for the wrong
reason, testing nothing. The signature policy itself is covered by the
signing_key tests below and by test_redteam_w5_cutoff_forgery.py.
"""
import hashlib
from datetime import date, datetime

import pytest

from tools.retrodiction.cutoff import (
    CutoffEnforcer,
    CutoffViolation,
    EvidenceRecord,
    PublicationProof,
    ProofKind,
)

CUTOFF = date(2024, 6, 1)


def _proof(content: str, published_on, kind=ProofKind.SOURCE_DECLARED,
           locator="acc-0001") -> PublicationProof:
    return PublicationProof(
        kind=kind,
        published_on=published_on,
        locator=locator,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def _rec(content="old data", published=date(2024, 3, 1), url="https://x/1",
         proof=None) -> EvidenceRecord:
    return EvidenceRecord(
        url=url, query="q", fetched_at=datetime(2026, 8, 22, 12, 0),
        content=content,
        proof=proof if proof is not None else _proof(content, published))


class TestAdmission:
    def test_pre_cutoff_verified_source_is_admitted(self):
        admitted, rejected = CutoffEnforcer(CUTOFF, allow_unsigned=True).admit([_rec()])
        assert len(admitted) == 1 and not rejected

    def test_fetch_date_is_irrelevant(self):
        # Fetched today, published before cutoff: fine.
        rec = _rec()
        rec.fetched_at = datetime.now()
        admitted, _ = CutoffEnforcer(CUTOFF, allow_unsigned=True).admit([rec])
        assert len(admitted) == 1

    def test_published_on_cutoff_day_is_excluded(self):
        # Strictly-before: equal dates leak post-cutoff intraday content.
        _, rejected = CutoffEnforcer(CUTOFF, allow_unsigned=True).admit(
            [_rec(published=CUTOFF)])
        assert len(rejected) == 1

    def test_post_cutoff_publication_is_excluded(self):
        _, rejected = CutoffEnforcer(CUTOFF, allow_unsigned=True).admit(
            [_rec(published=date(2024, 7, 15))])
        assert len(rejected) == 1


class TestLeakAttempts:
    """Each of these is a realistic leakage vector; all must be caught."""

    def test_no_proof_is_excluded_not_assumed_safe(self):
        rec = _rec(published=date(2020, 1, 1))
        rec.proof = None
        _, rejected = CutoffEnforcer(CUTOFF, allow_unsigned=True).admit([rec])
        assert len(rejected) == 1
        assert "no publication proof" in rejected[0].args[0]

    def test_edited_page_fails_byte_check(self):
        # Page was pre-cutoff when proven, then edited after the cutoff; the
        # fetch returned the EDITED bytes. The proof no longer covers them.
        old = "2024 guidance: strong"
        edited = old + "\nRevised after Q2 blowout"
        rec = EvidenceRecord(
            url="https://ir.example.com/guidance", query="guidance",
            fetched_at=datetime(2026, 8, 22), content=edited,
            proof=_proof(old, date(2024, 5, 1)))
        _, rejected = CutoffEnforcer(CUTOFF, allow_unsigned=True).admit([rec])
        assert len(rejected) == 1
        assert "different bytes" in rejected[0].args[0]

    def test_api_returning_current_values_under_historical_query(self):
        # The API call *looks* historical but the payload embeds today's value.
        content = '{"series": [101.3], "as_of": "2026-08-01"}'
        rec = EvidenceRecord(
            url="https://api.example.com/fred?obs_start=2024-05-01",
            query="historical CPI", fetched_at=datetime(2026, 8, 22),
            content=content, proof=None)
        _, rejected = CutoffEnforcer(CUTOFF, allow_unsigned=True).admit([rec])
        assert len(rejected) == 1

    def test_forged_date_caught_by_signature(self):
        # Attack: take a genuinely signed pre-cutoff proof and tamper with
        # published_on / bytes afterwards. The attacker cannot re-sign.
        good = _rec(url="https://x/ok")
        signed = good.proof.sign("harness-key")
        from dataclasses import replace
        tampered = replace(signed, published_on=date(2024, 5, 1))
        rec = _rec(published=date(2024, 5, 1), url="https://x/evil")
        rec.proof = tampered
        enforcer = CutoffEnforcer(CUTOFF, signing_key="harness-key")
        _, rejected = enforcer.admit([rec])
        assert len(rejected) == 1
        assert "signature" in rejected[0].args[0]
        # The untampered signed proof passes.
        admitted, _ = enforcer.admit([_rec(url="https://x/ok2").__class__(
            url="https://x/ok2", query="q",
            fetched_at=datetime(2026, 8, 22), content=_rec().content,
            proof=signed)])
        assert len(admitted) == 1

    def test_unsigned_proof_rejected_when_signing_enforced(self):
        rec = _rec()  # valid bytes match, but no signature
        _, rejected = CutoffEnforcer(CUTOFF, signing_key="k").admit([rec])
        assert len(rejected) == 1
        assert "signature" in rejected[0].args[0]

    def test_wrong_key_signature_rejected(self):
        rec = _rec()
        rec.proof = rec.proof.sign("attacker-key")
        _, rejected = CutoffEnforcer(CUTOFF, signing_key="harness-key").admit(
            [rec])
        assert len(rejected) == 1

    def test_unparseable_date_is_excluded(self):
        proof = PublicationProof(kind=ProofKind.EMBEDDED_METADATA,
                                 published_on="sometime in spring",
                                 locator="loc",
                                 content_sha256="0" * 64)
        rec = _rec(proof=proof)
        _, rejected = CutoffEnforcer(CUTOFF, allow_unsigned=True).admit([rec])
        assert len(rejected) == 1

    def test_bad_hash_format_rejected_at_construction(self):
        with pytest.raises(ValueError):
            PublicationProof(kind=ProofKind.SOURCE_DECLARED,
                             published_on=date(2024, 1, 1),
                             locator="l", content_sha256="deadbeef")

    def test_empty_locator_rejected(self):
        with pytest.raises(ValueError):
            PublicationProof(kind=ProofKind.SOURCE_DECLARED,
                             published_on=date(2024, 1, 1),
                             locator="", content_sha256="0" * 64)


class TestPolicy:
    def test_require_admitted_raises_when_anything_rejected(self):
        bad = _rec(published=date(2025, 1, 1))
        good = _rec(url="https://x/ok")
        with pytest.raises(CutoffViolation):
            CutoffEnforcer(CUTOFF, allow_unsigned=True).require_admitted([good, bad])

    def test_admitted_set_never_contains_a_violation(self):
        records = [_rec(), _rec(published=CUTOFF), _rec(proof=None),
                   _rec(published=date(2030, 1, 1))]
        enforcer = CutoffEnforcer(CUTOFF, allow_unsigned=True)
        admitted, rejected = enforcer.admit(records)
        assert len(admitted) + len(rejected) == len(records)
        assert all(r.proof is not None and
                   r.proof.published_on < CUTOFF and
                   r.proof.content_sha256 == r.content_sha256
                   for r in admitted)

    def test_cutoff_requires_a_date(self):
        with pytest.raises(TypeError):
            CutoffEnforcer("2024-06-01")
