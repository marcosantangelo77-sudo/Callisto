"""
Long-lived claims — the capability request/response frameworks cannot have.

A claim is not an answer; it is a POSITION that stays open for months,
accruing evidence, changing status, and eventually resolving. This module:

  1. Attaches new evidence to an OPEN claim and recomputes its confidence
     through the EXISTING provenance + clamp machinery (agp.thresholds
     MAX_CONFIDENCE_BY_SOURCE, agp.ConfidenceTier) — never a new formula.
  2. Records every confidence change as a BeliefRecord: what we believed,
     when, at what score/tier, on the basis of which evidence, and WHY it
     moved. The history IS the calibration record.
  3. Persists to a tamper-evident file store (JSONL append-only journal per
     claim + snapshot), so closing and reopening weeks later preserves full
     state and any retroactive edit breaks the chain hash.

Domain-general throughout: nothing here knows what domain the claim lives in.
Storage is file-backed by design: the DB migration is written up as a PROPOSAL
(findings/p2_claims_migration.md) against the tools/schema.py core/plugin seam.

Nothing here touches the live execution path or weakens any gate — confidence
can only be clamped DOWN by source class, and resolution requires an explicit
outcome, never inference from convenience.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Optional

from agp import ConfidenceTier, Domain, Evidence, SourceClass, _seal_digest
from agp.preregistration import Preregistration, Verdict
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE

_log = logging.getLogger("callisto.agp.claims")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClaimStatus(str, Enum):
    DRAFT = "draft"              # preregistration not yet sealed
    OPEN = "open"                # sealed preregistration, accruing evidence
    SUSPENDED = "suspended"      # paused, still accrues history
    CONFIRMED = "confirmed"      # resolved per preregistered criteria
    REFUTED = "refuted"
    AMBIGUOUS = "ambiguous"      # resolved without decisive verdict
    RETRACTED = "retracted"      # withdrawn by an agent, reason recorded


class ClaimError(Exception):
    pass


# ── Evidence accrual ─────────────────────────────────────────────────────

@dataclass
class AttachedEvidence:
    """One piece of evidence attached to an open claim. Wraps agp.Evidence
    plus the provenance-assigned class at attach time."""
    evidence: Evidence
    assigned_class: SourceClass        # from ProvenanceLedger, not self-declared
    attached_at: str = field(default_factory=_now_iso)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "evidence": self.evidence.to_dict(),
            "assigned_class": self.assigned_class.value,
            "attached_at": self.attached_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AttachedEvidence":
        return cls(
            evidence=Evidence(
                content=d["evidence"]["content"],
                source_class=SourceClass(d["evidence"]["source_class"]),
                confidence_score=d["evidence"]["confidence_score"],
                domain=Domain(d["evidence"]["domain"]),
                origin_agent=d["evidence"]["origin_agent"],
                source_name=d["evidence"].get("source_name", ""),
                timestamp=d["evidence"].get("timestamp", ""),
            ),
            assigned_class=SourceClass(d["assigned_class"]),
            attached_at=d.get("attached_at", ""),
            note=d.get("note", ""),
        )


_CLASS_RANK = {c: i for i, c in enumerate(
    (SourceClass.INFERRED, SourceClass.SIGNAL, SourceClass.SECONDARY,
     SourceClass.PRIMARY))}


@dataclass
class BeliefRecord:
    """One point in the calibration record: what we believed, when, why."""
    at: str
    confidence: float
    tier: str
    basis_evidence_count: int
    basis_best_class: str          # provenance-assigned best class so far
    change_reason: str             # e.g. "evidence_attached", "resolution",
                                   #      "contradiction_penalty", "initial"
    detail: str = ""
    prev_confidence: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "at": self.at, "confidence": self.confidence, "tier": self.tier,
            "basis_evidence_count": self.basis_evidence_count,
            "basis_best_class": self.basis_best_class,
            "change_reason": self.change_reason, "detail": self.detail,
            "prev_confidence": self.prev_confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BeliefRecord":
        return cls(**d)


def recompute_confidence(evidence_items: Iterable[AttachedEvidence],
                         claimed: float) -> float:
    """Clamp a claimed confidence to what the accrued evidence warrants.

    Uses the EXISTING ceiling table keyed by the best PROVENANCE-assigned
    class across all attached evidence. No new math: identical behavior to
    orchestrator's clamp, applied over the claim's lifetime corpus.
    """
    items = list(evidence_items)
    if not items:
        return round(min(max(claimed, 0.30), MAX_CONFIDENCE_BY_SOURCE["INFERRED"]), 2)
    best = max((it.assigned_class for it in items),
               key=lambda c: _CLASS_RANK[c])
    ceiling = MAX_CONFIDENCE_BY_SOURCE.get(best.value, 0.55)
    return round(min(max(float(claimed), 0.30), ceiling), 2)


class Claim:
    """A long-lived falsifiable position with full belief history."""

    def __init__(self, text: str, domain: Domain = Domain.GENERAL,
                 claim_id: Optional[str] = None):
        self.claim_id = claim_id or hashlib.sha256(
            f"{text}|{_now_iso()}".encode()).hexdigest()[:16]
        self.text = text
        self.domain = domain
        self.status = ClaimStatus.DRAFT
        self.created_at = _now_iso()
        self.updated_at = self.created_at
        self.evidence: list[AttachedEvidence] = []
        self.belief_history: list[BeliefRecord] = []
        self.confidence: float = 0.0
        self.preregistration: Optional[Preregistration] = None
        self.resolution: Optional[dict] = None   # verdict, when, divergences
        self.retraction: Optional[dict] = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def seal_preregistration(self, prereg: Preregistration) -> str:
        """A claim becomes OPEN only when its preregistration is SEALED.
        You cannot open a claim about the world without having committed,
        in writing, to what would confirm and refute it."""
        if self.status != ClaimStatus.DRAFT:
            raise ClaimError(f"claim already {self.status.value}")
        seal = prereg.seal() if not prereg.seal_hash else prereg.seal_hash
        self.preregistration = prereg
        self.status = ClaimStatus.OPEN
        self._touch()
        self.confidence = 0.30
        self._record_belief(self.confidence, "initial",
                            detail=f"opened under prereg seal {seal[:12]}")
        return seal

    def suspend(self, reason: str) -> None:
        self._require_open()
        if not (reason or "").strip():
            raise ClaimError("suspension requires a reason")
        self.status = ClaimStatus.SUSPENDED
        self._touch()

    def resume(self) -> None:
        if self.status != ClaimStatus.SUSPENDED:
            raise ClaimError("only suspended claims can resume")
        self.status = ClaimStatus.OPEN
        self._touch()

    # ── evidence accrual ────────────────────────────────────────────────

    def attach_evidence(self, evidence: Evidence,
                        assigned_class: Optional[SourceClass] = None,
                        note: str = "") -> BeliefRecord:
        """Attach one item to an OPEN claim and recompute confidence through
        the existing clamp machinery. ``assigned_class`` defaults to the
        declared class but callers SHOULD pass the ProvenanceLedger's
        assignment — a self-declared class never RAISES the ceiling here:
        passing None clamps to min(declared, ledger-less default SIGNAL)."""
        if self.status not in (ClaimStatus.OPEN, ClaimStatus.SUSPENDED):
            raise ClaimError(
                f"cannot attach evidence to a {self.status.value} claim")
        if assigned_class is None:
            # Fail toward the WEAKER class: unverified provenance must not
            # let INFERRED bytes masquerade as PRIMARY.
            rank = _CLASS_RANK[evidence.source_class]
            assigned_class = (evidence.source_class
                              if rank <= _CLASS_RANK[SourceClass.SIGNAL]
                              else SourceClass.SIGNAL)
        att = AttachedEvidence(evidence=evidence, assigned_class=assigned_class,
                               note=note)
        prev = self.confidence
        self.evidence.append(att)
        self.confidence = recompute_confidence(self.evidence,
                                               evidence.confidence_score)
        rec = self._record_belief(
            self.confidence, "evidence_attached",
            detail=f"attached via {assigned_class.value}: "
                   f"{(note or evidence.content)[:80]}",
            prev=prev)
        self._touch()
        return rec

    def apply_contradiction_penalty(self, severity: str, detail: str = "") \
            -> BeliefRecord:
        """Lower confidence using the existing CONTRADICTION_PENALTY table.
        Penalties only lower — nothing in this module may raise beyond the
        evidence-class ceiling, and this path exists so contradictions are
        recorded in the belief history rather than averaged away."""
        from agp.thresholds import CONTRADICTION_PENALTY
        if severity not in CONTRADICTION_PENALTY:
            raise ClaimError(f"unknown severity {severity!r}")
        prev = self.confidence
        self.confidence = round(
            max(0.30, prev - CONTRADICTION_PENALTY[severity]), 2)
        rec = self._record_belief(self.confidence, "contradiction_penalty",
                                  detail=f"{severity}: {detail}", prev=prev)
        self._touch()
        return rec

    # ── resolution ──────────────────────────────────────────────────────

    def resolve(self, *, observed_text: str = "",
                observed_value: Optional[float] = None,
                now: Optional[datetime] = None) -> dict:
        """Score reality against the SEALED criteria and close the claim.
        Resolution always runs through the preregistration — there is no
        path to CONFIRMED/REFUTED that bypasses the sealed criteria."""
        self._require_open()
        out = self.preregistration.score(
            observed_text=observed_text, observed_value=observed_value,
            evidence_count=len(self.evidence),
            best_source_class=self._best_assigned_class().value,
            now=now)
        verdict_to_status = {Verdict.CONFIRMED: ClaimStatus.CONFIRMED,
                             Verdict.REFUTED: ClaimStatus.REFUTED,
                             Verdict.AMBIGUOUS: ClaimStatus.AMBIGUOUS}
        self.status = verdict_to_status[out.verdict]
        self.resolution = {
            "verdict": out.verdict.value,
            "divergences": out.divergences,
            "scored_against_seal": out.scored_against_seal,
            "resolved_at": _now_iso(),
        }
        prev = self.confidence
        self.confidence = {"CONFIRMED": min(max(prev, 0.75), 1.0),
                           "REFUTED": 0.30,
                           "AMBIGUOUS": min(prev, 0.55)}[out.verdict.value]
        self._record_belief(self.confidence, "resolution",
                            detail=f"verdict {out.verdict.value} against "
                                   f"sealed criteria "
                                   f"({len(out.divergences)} divergence(s))",
                            prev=prev)
        self._touch()
        for d in out.divergences:
            _log.warning("[claim %s divergence] %s", self.claim_id, d)
        return self.resolution

    def retract(self, reason: str) -> None:
        if self.resolution:
            raise ClaimError("cannot retract a resolved claim — record a new "
                             "claim instead; history stays intact")
        if not (reason or "").strip():
            raise ClaimError("retraction requires a stated reason")
        prev = self.confidence
        self.status = ClaimStatus.RETRACTED
        self.retraction = {"reason": reason, "at": _now_iso()}
        self._record_belief(0.30, "retraction", detail=reason, prev=prev)
        self._touch()

    # ── queries ─────────────────────────────────────────────────────────

    def belief_timeline(self) -> list[BeliefRecord]:
        """What did we believe, when, and on what basis? Ordered oldest-first;
        this IS the calibration record."""
        return list(self.belief_history)

    def _best_assigned_class(self) -> SourceClass:
        if not self.evidence:
            return SourceClass.INFERRED
        return max((a.assigned_class for a in self.evidence),
                   key=lambda c: _CLASS_RANK[c])

    def _require_open(self) -> None:
        if self.status not in (ClaimStatus.OPEN, ClaimStatus.SUSPENDED):
            raise ClaimError(f"claim is {self.status.value}, not open")

    def _record_belief(self, conf: float, reason: str, detail: str = "",
                       prev: Optional[float] = None) -> BeliefRecord:
        rec = BeliefRecord(
            at=_now_iso(), confidence=conf,
            tier=ConfidenceTier.from_score(conf).value,
            basis_evidence_count=len(self.evidence),
            basis_best_class=self._best_assigned_class().value,
            change_reason=reason, detail=detail, prev_confidence=prev)
        self.belief_history.append(rec)
        return rec

    def _touch(self) -> None:
        self.updated_at = _now_iso()

    # ── persistence ─────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id, "text": self.text,
            "domain": self.domain.value, "status": self.status.value,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "confidence": self.confidence,
            "evidence": [e.to_dict() for e in self.evidence],
            "belief_history": [b.to_dict() for b in self.belief_history],
            "preregistration":
                self.preregistration.to_dict() if self.preregistration else None,
            "resolution": self.resolution,
            "retraction": self.retraction,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        c = cls(text=d["text"], domain=Domain(d["domain"]),
                claim_id=d["claim_id"])
        c.status = ClaimStatus(d["status"])
        c.created_at = d["created_at"]
        c.updated_at = d["updated_at"]
        c.confidence = float(d["confidence"])
        c.evidence = [AttachedEvidence.from_dict(e) for e in d["evidence"]]
        c.belief_history = [BeliefRecord.from_dict(b)
                            for b in d["belief_history"]]
        c.preregistration = (Preregistration.from_dict(d["preregistration"])
                             if d.get("preregistration") else None)
        c.resolution = d.get("resolution")
        c.retraction = d.get("retraction")
        return c


# ── Tamper-evident store ─────────────────────────────────────────────────

class ClaimStore:
    """File-backed claim persistence with a hash-chained JSONL journal.

    Every mutation appends one journal line containing the full claim state
    after the change plus the sha256 of the PREVIOUS line — a retroactive
    edit anywhere breaks every later link. ``load`` verifies the whole chain
    before returning state, so tampering is detected on read, loudly.
    """

    def __init__(self, directory: str):
        self._dir = directory
        os.makedirs(self._dir, exist_ok=True)

    def _journal_path(self, claim_id: str) -> str:
        if not claim_id or any(ch in claim_id for ch in "/\\."):
            raise ValueError(f"invalid claim_id {claim_id!r}")
        return os.path.join(self._dir, f"claim_{claim_id}.jsonl")

    @staticmethod
    def _entry_seal(prev_hash: str, saved_at: str, state: dict) -> str:
        """Keyed integrity seal over an entry's canonical content.

        Uses the project's existing _seal_digest machinery (HMAC-SHA256 when
        CALLISTO_SEAL_KEY is set). Sealing {prev, saved_at, state} binds EVERY
        entry's content — including a single-entry journal's first line and
        every tail entry, which no successor exists to protect.
        """
        payload = json.dumps(
            {"prev": prev_hash, "saved_at": saved_at, "state": state},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return _seal_digest(payload)

    def save(self, claim: Claim) -> int:
        """Append current state to the journal. Returns new entry count."""
        path = self._journal_path(claim.claim_id)
        prev_hash = "GENESIS"
        count = 0
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            count = len(lines)
            if lines:
                prev_hash = hashlib.sha256(
                    lines[-1].encode("utf-8")).hexdigest()
        saved_at = _now_iso()
        state = claim.to_dict()
        entry = {"prev": prev_hash, "saved_at": saved_at,
                 "state": state,
                 "seal": self._entry_seal(prev_hash, saved_at, state)}
        blob = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(blob + "\n")
        return count + 1

    def load(self, claim_id: str, verify: bool = True,
             allow_legacy_unsigned: bool = False) -> Optional[Claim]:
        """Replay the journal to latest verified state.

        With verify=True (default) any break in the hash chain or ANY invalid
        or missing per-entry integrity seal raises ClaimError — a tampered or
        flattering history never silently loads.

        Legacy policy (explicit, opt-in): journals written before per-entry
        sealing carry only ``prev`` pointers; their FIRST/TAIL entries were
        unverifiable. Callers may pass allow_legacy_unsigned=True to read such
        journals — chain checks still apply, but their tail integrity is NOT
        guaranteed and this must never be enabled on security-sensitive
        paths. Unsigned entries fail closed by default.
        """
        path = self._journal_path(claim_id)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        prev = "GENESIS"
        state = None
        for i, ln in enumerate(lines):
            try:
                entry = json.loads(ln)
            except json.JSONDecodeError as e:
                raise ClaimError(f"journal line {i+1} corrupt: {e}")
            if verify:
                # Chain check: line 1 must reference GENESIS; each later
                # line must reference its predecessor's raw-line digest.
                if entry.get("prev") != prev:
                    raise ClaimError(
                        f"tampering detected in claim {claim_id}: journal "
                        f"line {i+1} does not chain to its predecessor "
                        f"(history is not trustworthy)")
                seal = entry.get("seal")
                if seal is None:
                    if not allow_legacy_unsigned:
                        raise ClaimError(
                            f"tampering detected in claim {claim_id}: "
                            f"journal line {i+1} has no integrity seal "
                            f"(legacy unsigned journal; refusing to load "
                            f"unverified history)")
                elif not hmac.compare_digest(
                        str(seal),
                        self._entry_seal(entry["prev"], entry["saved_at"],
                                         entry["state"])):
                    raise ClaimError(
                        f"tampering detected in claim {claim_id}: journal "
                        f"line {i+1} failed its integrity seal (content "
                        f"does not match the sealed record)")
            prev = hashlib.sha256(ln.encode("utf-8")).hexdigest()
            state = entry["state"]
        return Claim.from_dict(state)

    def list_ids(self) -> list[str]:
        prefix, suffix = "claim_", ".jsonl"
        out = []
        for name in sorted(os.listdir(self._dir)):
            if name.startswith(prefix) and name.endswith(suffix):
                out.append(name[len(prefix):-len(suffix)])
        return out
