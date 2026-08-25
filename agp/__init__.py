"""
Aluft Gianne Protocol — core module.

Enums, dataclasses, and session lifecycle for the AGP 7-step research methodology.
"""

import hashlib
import hmac
import json
import logging
import os
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


# ── Seal keying ──────────────────────────────────────────────────────────
# An unkeyed SHA-256 seal is not a seal — anyone with DB write access can
# recompute it over tampered bytes (verify_seal is public code). A keyed
# HMAC makes forgery require the key, not just the repo.
#
#   CALLISTO_SEAL_KEY  — hex-encoded secret; when set, seals are HMAC-SHA256.
#   When unset, seals fall back to unkeyed SHA-256 for backward compatibility
#   with existing sealed sessions (legacy seals still verify; new seals are
#   unkeyed and remain forgeable — set the key to close that hole).
#
# Key rotation: verify tries the current key first, then the legacy unkeyed
# digest, then any key listed in CALLISTO_SEAL_KEY_OLD (comma-separated hex).
def _seal_keys() -> list[bytes]:
    keys: list[bytes] = []
    current = os.getenv("CALLISTO_SEAL_KEY", "").strip()
    if current:
        try:
            keys.append(bytes.fromhex(current))
        except ValueError:
            logging.getLogger("callisto.agp").error(
                "CALLISTO_SEAL_KEY is not valid hex — falling back to unkeyed seal"
            )
    for old in os.getenv("CALLISTO_SEAL_KEY_OLD", "").split(","):
        old = old.strip()
        if old:
            try:
                keys.append(bytes.fromhex(old))
            except ValueError:
                pass
    return keys


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


def _seal_digest(payload: str) -> str:
    """Compute the seal digest over a canonical payload.

    HMAC-SHA256 with CALLISTO_SEAL_KEY when set (forgery now requires the
    key); unkeyed SHA-256 fallback for backward compatibility with legacy
    sealed sessions when no key is configured.
    """
    keys = _seal_keys()
    if keys:
        return hmac.new(keys[0], payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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

        # Independent seal reviewer (mechanism 4 of the earned-confidence
        # design): a callable(session, summary) → Optional[str]. Non-empty /
        # truthy return = veto reason; the seal is refused. This is the hook
        # that lets a component which did NOT write the conclusion (the
        # Sentinel) block a seal on "conclusion asserts X, evidence contains
        # no X". None/absent = legacy behavior, nothing changes.
        self.seal_veto = None

        self._sealed: bool = False

        # Liveness tracking — lets an external watcher (task_worker adaptive
        # timeout) decide "this session is making progress, extend the
        # budget" vs "this session has stalled, cut it off at the boundary".
        # Monotonic clock so the comparison is robust against wall-clock jumps.
        # Initialized to "now" so a freshly-created session is considered
        # alive by any watcher that polls before the first step completes.
        import time as _time
        self._t_created_monotonic: float = _time.monotonic()
        self.last_progress_at: float = self._t_created_monotonic
        self.last_step_at: float = self._t_created_monotonic
        # Total count of discrete progress events (step advances + evidence
        # adds + contradiction adds). Exposed in to_dict() for debuggability.
        self.progress_events: int = 0

        # A20: the quantitative artifacts a conclusion cites (charts,
        # workbooks, sandbox outputs) are part of what the seal must cover.
        # Empty by default — legacy callers seal exactly as before — but the
        # field is ALWAYS present in to_dict() so the keyed-HMAC payload
        # structurally cannot omit the artifact layer again.
        self.artifact_refs: list = []

    def add_artifacts(self, refs: list) -> None:
        """Attach artifact refs so they are covered by the seal payload."""
        if self._sealed:
            raise AGPViolation("Cannot add artifacts to a sealed session")
        self.artifact_refs.extend(refs)

    def _mark_progress(self, step_advance: bool = False) -> None:
        """Called internally whenever something observable changes.

        Separated so tests can monkey-patch timing without touching internal
        bookkeeping. step_advance=True also bumps last_step_at so the
        task_worker can distinguish "phase moved" from "more evidence in same
        phase".
        """
        import time as _time
        now = _time.monotonic()
        self.last_progress_at = now
        if step_advance:
            self.last_step_at = now
        self.progress_events += 1

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
        self._mark_progress(step_advance=True)

    def add_evidence(self, evidence: Evidence) -> None:
        """Add evidence, filtering out non-storable items.

        Filtered (UNVERIFIED) items increment filtered_evidence_count so seal()
        can detect sessions where the majority of evidence was rejected.
        """
        if self._sealed:
            raise AGPViolation("Cannot add evidence to a sealed session")
        # Mark progress even for filtered evidence — an UNVERIFIED add still
        # proves the session is actively doing work (tool calls returned,
        # even if the result was rejected). Silent-stall is the only state
        # we want to timeout on.
        self._mark_progress()
        if not evidence.confidence_tier.is_storable:
            self.filtered_evidence_count += 1
            return  # filtered per AGP rules (was silent pre-rigor fix)
        self.evidence.append(evidence)

    def add_contradiction(self, contradiction: Contradiction) -> None:
        if self._sealed:
            raise AGPViolation("Cannot add contradictions to a sealed session")
        self.contradictions.append(contradiction)
        self._mark_progress()

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
            "progress_events": self.progress_events,
            # A20: artifact refs ride inside the sealed payload. Serialized
            # as full ref dicts (not truncated ids) so a seal verifier can
            # re-hash the cited bytes against the store.
            "artifact_refs": [
                r.to_dict() if hasattr(r, "to_dict") else dict(r)
                for r in self.artifact_refs
            ],
        }

    def seal(self) -> str:
        """Seal the session with a SHA-256 hash of its canonical JSON payload.

        Refuses to seal garbage:
          - conclusion is empty or == EMPTY_SYNTHESIS_MARKER
          - len(evidence) == 0
          - filtered_evidence_count > len(evidence)  (mostly-rejected session)
          - self.seal_veto(session, summary) returns a truthy reason — the
            independent-reviewer hook (e.g. Sentinel fed conclusion +
            evidence). Reviewer exceptions FAIL CLOSED (refuse).

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

        # ── Independent reviewer veto (fails closed) ──
        if self.seal_veto is not None:
            try:
                reason = self.seal_veto(self, self.summary)
            except Exception as e:
                reason = f"reviewer crashed: {type(e).__name__}: {e}"
                logger.error(
                    "AGP seal veto reviewer raised for session %s — failing closed",
                    self.session_id,
                )
            if reason:
                logger.warning(
                    "AGP seal refused for session %s by independent review: %s",
                    self.session_id, reason,
                )
                raise AGPSealRefused(
                    f"Session {self.session_id}: refusing to seal — "
                    f"independent review veto: {reason}"
                )

        self.sealed_at = datetime.now(timezone.utc).isoformat()
        payload = _canonical_payload(self.to_dict())
        self.seal_hash = _seal_digest(payload)
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
        return seal_verification_method(stored) is not None


def seal_verification_method(stored: Union[dict, str]) -> Optional[str]:
    """Which digest family verified this stored session, if any.

    Returns "keyed" when an HMAC key (current or rotation) verified it,
    "unkeyed" when only the legacy public SHA-256 did, and None when nothing
    verified — missing seal_hash, malformed input, or tampered content.
    Never raises. verify_seal() is exactly ``... is not None``; the split
    exists so a verifier can REPORT how much trust the seal carries instead
    of collapsing keyed and unkeyed into one boolean.
    """
    try:
        if isinstance(stored, str):
            data = json.loads(stored)
        else:
            data = dict(stored)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    stored_hash = data.get("seal_hash")
    if not stored_hash or not isinstance(stored_hash, str):
        return None

    payload = _canonical_payload(data)
    # Constant-time comparisons throughout; a digest can match at most one
    # family, so check order affects only the reported label, never the verdict.
    unkeyed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if hmac.compare_digest(unkeyed, stored_hash):
        return "unkeyed"
    for key in _seal_keys():
        keyed = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(keyed, stored_hash):
            return "keyed"
    return None


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
