"""Tamper-detection and refuse-to-seal tests for AGP sessions.

Covers the rigor upgrade: verify_seal() against tampered payloads, and
seal() refusing garbage sessions (empty conclusion, zero evidence, mostly
filtered evidence).
"""

import copy
import json

import pytest

from agp import (
    AGPSealRefused,
    AGPSession,
    AGPViolation,
    ConfidenceTier,
    Domain,
    EMPTY_SYNTHESIS_MARKER,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)


def _build_sealable_session() -> AGPSession:
    """Walk a session through all steps with real evidence + a real conclusion
    so seal() accepts it. Returns the sealed session."""
    s = AGPSession("what is the airspeed velocity of an unladen swallow?")
    s.domain = Domain.TECHNICAL
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["holy-grail.org"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="African swallow is ~24 mph",
        source_class=SourceClass.SECONDARY,
        confidence_score=0.72,
        domain=Domain.TECHNICAL,
        origin_agent="test",
        source_name="https://example.org/swallow",
    ))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope=s.scope,
        domain=Domain.TECHNICAL,
        conclusion="African (non-European) swallow airspeed is ~24 mph unladen.",
        confidence_score=0.7,
        evidence_count=1,
        contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    s.seal()
    return s


class TestVerifySealHappyPath:
    def test_seal_verifies(self):
        s = _build_sealable_session()
        assert s.seal_hash is not None and len(s.seal_hash) == 64
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_verify_accepts_json_string(self):
        s = _build_sealable_session()
        as_json = json.dumps(s.to_dict())
        assert AGPSession.verify_seal(as_json) is True


class TestVerifySealTamper:
    def test_tampered_conclusion_rejected(self):
        s = _build_sealable_session()
        data = s.to_dict()
        data["summary"]["conclusion"] = "Actually the swallow flies at 500 mph."
        assert AGPSession.verify_seal(data) is False

    def test_tampered_confidence_rejected(self):
        s = _build_sealable_session()
        data = s.to_dict()
        data["summary"]["confidence_score"] = 0.99
        assert AGPSession.verify_seal(data) is False

    def test_tampered_evidence_rejected(self):
        s = _build_sealable_session()
        data = s.to_dict()
        data["evidence"][0]["content"] = "Swallow actually flies at 5 mph"
        assert AGPSession.verify_seal(data) is False

    def test_missing_seal_hash_rejected(self):
        s = _build_sealable_session()
        data = s.to_dict()
        del data["seal_hash"]
        assert AGPSession.verify_seal(data) is False

    def test_null_seal_hash_rejected(self):
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = None
        assert AGPSession.verify_seal(data) is False

    def test_corrupt_json_rejected(self):
        assert AGPSession.verify_seal("{not json") is False

    def test_non_dict_rejected(self):
        assert AGPSession.verify_seal(None) is False  # type: ignore[arg-type]
        assert AGPSession.verify_seal(42) is False  # type: ignore[arg-type]

    def test_tampered_sealed_at_rejected(self):
        """sealed_at IS part of the hash (set before hashing in seal()),
        so tampering with it must fail verification."""
        s = _build_sealable_session()
        data = s.to_dict()
        data["sealed_at"] = "2099-12-31T23:59:59Z"
        assert AGPSession.verify_seal(data) is False

    def test_tampered_filtered_count_rejected(self):
        """filtered_evidence_count is part of the hashed payload."""
        s = _build_sealable_session()
        data = s.to_dict()
        data["filtered_evidence_count"] = 99
        assert AGPSession.verify_seal(data) is False


class TestRefuseToSeal:
    def _base_session(self, conclusion: str = "real conclusion",
                      evidence_count: int = 1) -> AGPSession:
        s = AGPSession("test scope")
        s.domain = Domain.GENERAL
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        for i in range(evidence_count):
            s.add_evidence(Evidence(
                content=f"fact {i}",
                source_class=SourceClass.SECONDARY,
                confidence_score=0.7,
                domain=Domain.GENERAL,
                origin_agent="test",
            ))
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope="test scope",
            domain=Domain.GENERAL,
            conclusion=conclusion,
            confidence_score=0.6,
            evidence_count=evidence_count,
            contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        return s

    def test_empty_evidence_refused(self):
        s = self._base_session(evidence_count=0)
        with pytest.raises(AGPSealRefused, match="zero evidence"):
            s.seal()

    def test_empty_conclusion_refused(self):
        s = self._base_session(conclusion="")
        with pytest.raises(AGPSealRefused, match="empty or default"):
            s.seal()

    def test_whitespace_conclusion_refused(self):
        s = self._base_session(conclusion="   \n\t  ")
        with pytest.raises(AGPSealRefused, match="empty or default"):
            s.seal()

    def test_default_marker_conclusion_refused(self):
        s = self._base_session(conclusion=EMPTY_SYNTHESIS_MARKER)
        with pytest.raises(AGPSealRefused, match="empty or default"):
            s.seal()

    def test_mostly_filtered_session_refused(self):
        """If more evidence was filtered (UNVERIFIED) than kept, refuse."""
        s = AGPSession("test")
        s.domain = Domain.GENERAL
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        # 1 kept
        s.add_evidence(Evidence(
            content="ok", source_class=SourceClass.SECONDARY,
            confidence_score=0.7, domain=Domain.GENERAL, origin_agent="t",
        ))
        # 3 filtered
        for _ in range(3):
            s.add_evidence(Evidence(
                content="weak", source_class=SourceClass.INFERRED,
                confidence_score=0.1, domain=Domain.GENERAL, origin_agent="t",
            ))
        assert len(s.evidence) == 1
        assert s.filtered_evidence_count == 3
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope="test", domain=Domain.GENERAL,
            conclusion="maybe",
            confidence_score=0.6, evidence_count=1, contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        with pytest.raises(AGPSealRefused, match="filtered"):
            s.seal()

    def test_filtered_count_included_in_to_dict(self):
        s = AGPSession("x")
        s.add_evidence(Evidence(
            content="weak", source_class=SourceClass.INFERRED,
            confidence_score=0.05, domain=Domain.GENERAL, origin_agent="t",
        ))
        assert s.to_dict()["filtered_evidence_count"] == 1


class TestCanonicalPayload:
    """The canonical payload normalizes seal_hash to None before hashing
    (it cannot be part of its own hash). sealed_at IS part of the hashed
    payload — it's set before hashing in seal()."""

    def test_seal_hash_normalized_to_none(self):
        from agp import _canonical_payload
        import json as _json
        s = _build_sealable_session()
        payload = _canonical_payload(s.to_dict())
        parsed = _json.loads(payload)
        assert parsed["seal_hash"] is None
        assert parsed["sealed_at"] is not None  # sealed_at IS hashed
        # Other fields preserved
        assert "evidence" in parsed
        assert "summary" in parsed
