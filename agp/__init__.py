"""
Aluft Gianne Protocol — core module.

Enums, dataclasses, and session lifecycle for the AGP 7-step research methodology.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Union

from agp.thresholds import (
    TIER_VERIFIED_MIN,
    TIER_CORROBORATED_MIN,
    TIER_PROBABLE_MIN,
    TIER_SPECULATIVE_MIN,
)

logger = logging.getLogger("callisto.agp")


# Sentinel value used when synthesis fails to produce anything useful.
# Sessions with this as their conclusion MUST NOT seal — they represent
# garbage synthesis that would otherwise write a 0.30 SPECULATIVE row to DB.
EMPTY_SYNTHESIS_MARKER = "No synthesis produced."


class Domain(str, Enum):
    FINANCIAL = "FINANCIAL"
    TECHNICAL = "TECHNICAL"
    SIGNAL = "SIGNAL"
    SYNTHESIS = "SYNTHESIS"
    GENERAL = "GENERAL"


class SourceClass(str, Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    SIGNAL = "SIGNAL"
    INFERRED = "INFERRED"


class ConfidenceTier(str, Enum):
    VERIFIED = "VERIFIED"
    CORROBORATED = "CORROBORATED"
    PROBABLE = "PROBABLE"
    SPECULATIVE = "SPECULATIVE"
    UNVERIFIED = "UNVERIFIED"

    @staticmethod
    def from_score(score: float) -> "ConfidenceTier":
        if score >= TIER_VERIFIED_MIN:
            return ConfidenceTier.VERIFIED
        elif score >= TIER_CORROBORATED_MIN:
            return ConfidenceTier.CORROBORATED
        elif score >= TIER_PROBABLE_MIN:
            return ConfidenceTier.PROBABLE
        elif score >= TIER_SPECULATIVE_MIN:
            return ConfidenceTier.SPECULATIVE
        else:
            return ConfidenceTier.UNVERIFIED

    @property
    def is_storable(self) -> bool:
        return self != ConfidenceTier.UNVERIFIED


class SessionStep(int, Enum):
    DECLARE_SCOPE = 1
    ASSIGN_DOMAIN = 2
    SOURCE_ENUMERATION = 3
    PRIMARY_COLLECTION = 4
    CONTRADICTION_CHECK = 5
    SYNTHESIS = 6
    SESSION_CLOSE = 7


class AGPViolation(Exception):
    """Raised when AGP protocol rules are violated."""
    pass


class AGPSealTampered(Exception):
    """Raised when a stored session's seal_hash does not match its re-computed hash.

    Signals either data corruption or active tampering. Callers loading sessions
    from memory MUST handle this — a tampered session is not a trusted session.
    """
    pass


class AGPSealRefused(Exception):
    """Raised when seal() refuses to seal a session due to garbage content.

    Conditions: empty conclusion, EMPTY_SYNTHESIS_MARKER conclusion, zero
    evidence, or filtered_evidence_count > kept evidence count.
    """
    pass


@dataclass
class Evidence:
    content: str
    source_class: SourceClass
    confidence_score: float
    domain: Domain
    origin_agent: str
    source_name: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def confidence_tier(self) -> ConfidenceTier:
        return ConfidenceTier.from_score(self.confidence_score)

    def can_promote_to_conclusion(self) -> bool:
        """SIGNAL evidence cannot be promoted without PRIMARY corroboration."""
        if self.source_class == SourceClass.SIGNAL:
            return False
        return self.confidence_tier.is_storable

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "source_class": self.source_class.value,
            "confidence_score": self.confidence_score,
            "confidence_tier": self.confidence_tier.value,
            "domain": self.domain.value,
            "origin_agent": self.origin_agent,
            "source_name": self.source_name,
            "timestamp": self.timestamp,
        }


@dataclass
class Contradiction:
    claim_a: str
    claim_b: str
    source_a: str
    source_b: str
    severity: str = "MINOR"
    resolution: str = ""

    def to_dict(self) -> dict:
        return {
            "claim_a": self.claim_a,
            "claim_b": self.claim_b,
            "source_a": self.source_a,
            "source_b": self.source_b,
            "severity": self.severity,
            "resolution": self.resolution,
        }


@dataclass
class SessionSummary:
    scope: str
    domain: Domain
    conclusion: str
    confidence_score: float
    evidence_count: int
    contradiction_count: int
    manager_objections: list[str] = field(default_factory=list)

    @property
    def confidence_tier(self) -> ConfidenceTier:
        return ConfidenceTier.from_score(self.confidence_score)

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "domain": self.domain.value,
            "conclusion": self.conclusion,
            "confidence_score": self.confidence_score,
            "confidence_tier": self.confidence_tier.value,
            "evidence_count": self.evidence_count,
            "contradiction_count": self.contradiction_count,
            "manager_objections": self.manager_objections,
        }


class AGPSession:
    """7-step AGP session lifecycle with strict sequential advancement."""

    def __init__(self, query: str):
        self.session_id: str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        self.query: str = query
        self.current_step: SessionStep = SessionStep.DECLARE_SCOPE
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.sealed_at: Optional[str] = None
        self.seal_hash: Optional[str] = None

        # Step outputs
        self.scope: str = query
        self.domain: Optional[Domain] = None
        self.sources: list[str] = []
        self.evidence: list[Evidence] = []
        self.contradictions: list[Contradiction] = []
        self.summary: Optional[SessionSummary] = None
        self.manager_objections: list[str] = []

        # Track how many Evidence items were dropped by add_evidence() because
        # they were UNVERIFIED. If this ever exceeds len(self.evidence) at seal
        # time, the session is mostly noise and seal() refuses.
        self.filtered_evidence_count: int = 0

        self._sealed: bool = False

    def advance_to(self, step: SessionStep) -> None:
        """Advance to the next step. Steps must proceed sequentially."""
        if self._sealed:
            raise AGPViolation("Cannot advance a sealed session")
        expected = SessionStep(self.current_step.value + 1)
        if step != expected:
            raise AGPViolation(
                f"Cannot advance from {self.current_step.name} to {step.name}; "
                f"expected {expected.name}"
            )
        self.current_step = step

    def add_evidence(self, evidence: Evidence) -> None:
        """Add evidence, filtering out non-storable items.

        Filtered (UNVERIFIED) items increment filtered_evidence_count so seal()
        can detect sessions where the majority of evidence was rejected.
        """
        if self._sealed:
            raise AGPViolation("Cannot add evidence to a sealed session")
        if not evidence.confidence_tier.is_storable:
            self.filtered_evidence_count += 1
            return  # filtered per AGP rules (was silent pre-rigor fix)
        self.evidence.append(evidence)

    def add_contradiction(self, contradiction: Contradiction) -> None:
        if self._sealed:
            raise AGPViolation("Cannot add contradictions to a sealed session")
        self.contradictions.append(contradiction)

    def add_manager_objection(self, objection: str) -> None:
        if self._sealed:
            raise AGPViolation("Cannot add objections to a sealed session")
        self.manager_objections.append(objection)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "query": self.query,
            "scope": self.scope,
            "domain": self.domain.value if self.domain else None,
            "sources": self.sources,
            "evidence": [e.to_dict() for e in self.evidence],
            "contradictions": [c.to_dict() for c in self.contradictions],
            "summary": self.summary.to_dict() if self.summary else None,
            "manager_objections": self.manager_objections,
            "started_at": self.started_at,
            "sealed_at": self.sealed_at,
            "seal_hash": self.seal_hash,
            "current_step": self.current_step.name,
            "filtered_evidence_count": self.filtered_evidence_count,
        }

    def seal(self) -> str:
        """Seal the session with a SHA-256 hash of its canonical JSON payload.

        Refuses to seal garbage:
          - conclusion is empty or == EMPTY_SYNTHESIS_MARKER
          - len(evidence) == 0
          - filtered_evidence_count > len(evidence)  (mostly-rejected session)

        Raises AGPSealRefused in those cases instead of sealing a 0.30
        SPECULATIVE row into the DB.
        """
        if self._sealed:
            raise AGPViolation("Session already sealed")
        if self.current_step != SessionStep.SESSION_CLOSE:
            raise AGPViolation(
                f"Cannot seal session at step {self.current_step.name}; "
                f"must be at SESSION_CLOSE"
            )
        if self.summary is None:
            raise AGPViolation("Cannot seal session without a summary")

        # ── Refuse-to-seal-garbage gates ──
        conclusion = (self.summary.conclusion or "").strip()
        if not conclusion or conclusion == EMPTY_SYNTHESIS_MARKER:
            logger.warning(
                "AGP seal refused for session %s: empty/default conclusion",
                self.session_id,
            )
            raise AGPSealRefused(
                f"Session {self.session_id}: refusing to seal — empty or default conclusion"
            )
        if len(self.evidence) == 0:
            logger.warning(
                "AGP seal refused for session %s: zero evidence items",
                self.session_id,
            )
            raise AGPSealRefused(
                f"Session {self.session_id}: refusing to seal — zero evidence"
            )
        if self.filtered_evidence_count > len(self.evidence):
            logger.warning(
                "AGP seal refused for session %s: filtered=%d > kept=%d",
                self.session_id,
                self.filtered_evidence_count,
                len(self.evidence),
            )
            raise AGPSealRefused(
                f"Session {self.session_id}: refusing to seal — "
                f"filtered ({self.filtered_evidence_count}) > kept ({len(self.evidence)})"
            )

        self.sealed_at = datetime.now(timezone.utc).isoformat()
        payload = _canonical_payload(self.to_dict())
        self.seal_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self._sealed = True
        return self.seal_hash

    @staticmethod
    def verify_seal(stored: Union[dict, str]) -> bool:
        """Recompute the SHA-256 over a stored session dict (or JSON string) minus
        seal_hash / sealed_at, and compare with the stored seal_hash.

        Returns True only if the hashes match. False on any failure — including
        missing seal_hash, malformed JSON, or tampered content. Never raises.

        Callers that want hard failure should raise AGPSealTampered themselves.
        """
        try:
            if isinstance(stored, str):
                data = json.loads(stored)
            else:
                data = dict(stored)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False

        stored_hash = data.get("seal_hash")
        if not stored_hash or not isinstance(stored_hash, str):
            return False

        payload = _canonical_payload(data)
        recomputed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return recomputed == stored_hash


def _canonical_payload(data: dict) -> str:
    """Build the canonical JSON payload hashed by seal()/verify_seal().

    Algorithm (preserved from pre-rigor era for backward compatibility with
    existing sealed sessions):
      - Normalize seal_hash → None (the field was always present at seal time
        with value None; it's the one field that cannot be part of its own hash)
      - Keep sealed_at as-is (set BEFORE the hash is computed)
      - All other fields are hashed by sort_keys=True JSON

    verify_seal() normalizes input the same way before comparing.
    """
    payload_dict = dict(data)
    payload_dict["seal_hash"] = None
    return json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)
