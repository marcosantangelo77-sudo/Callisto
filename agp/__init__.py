"""
Aluft Gianne Protocol — core module.

Enums, dataclasses, and session lifecycle for the AGP 7-step research methodology.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


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
        if score >= 0.90:
            return ConfidenceTier.VERIFIED
        elif score >= 0.75:
            return ConfidenceTier.CORROBORATED
        elif score >= 0.55:
            return ConfidenceTier.PROBABLE
        elif score >= 0.30:
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
        """Add evidence, filtering out non-storable items."""
        if self._sealed:
            raise AGPViolation("Cannot add evidence to a sealed session")
        if not evidence.confidence_tier.is_storable:
            return  # silently filtered per AGP rules
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
        }

    def seal(self) -> str:
        """Seal the session with a SHA-256 hash of its canonical JSON payload."""
        if self._sealed:
            raise AGPViolation("Session already sealed")
        if self.current_step != SessionStep.SESSION_CLOSE:
            raise AGPViolation(
                f"Cannot seal session at step {self.current_step.name}; "
                f"must be at SESSION_CLOSE"
            )
        if self.summary is None:
            raise AGPViolation("Cannot seal session without a summary")

        self.sealed_at = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        self.seal_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self._sealed = True
        return self.seal_hash
