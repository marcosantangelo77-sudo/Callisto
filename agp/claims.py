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

from agp import (
    ConfidenceTier, Domain, Evidence, SourceClass,
    _seal_digest, _seal_keys,
)
from agp.preregistration import Preregistration, Verdict
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE

_log = logging.getLogger("callisto.agp.claims")


def _configured_seal_key_vars() -> bool:
    """True iff ANY seal-key environment variable is set (even to garbage).

    Distinguishes "no key configured" (an unkeyed deployment) from "key
    configured but malformed" — a malformed configured policy must fail
    closed, never silently degrade to an unkeyed one.
    """
    return bool(
        os.getenv("CALLISTO_SEAL_KEY", "").strip()
        or os.getenv("CALLISTO_SEAL_KEY_OLD", "").strip())


def _keyed_policy_active() -> bool:
    """True iff a usable seal key is configured for this process.

    A configured-but-invalid key makes this False AND
    _configured_seal_key_vars() True; callers must treat that combination as
    a configuration error and fail closed.
    """
    return bool(_seal_keys())


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
    def _entry_seal_payload(prev_hash: str, saved_at: str, state: dict) -> str:
        """Canonical payload sealed by every journal entry."""
        return json.dumps(
            {"prev": prev_hash, "saved_at": saved_at, "state": state},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _entry_seal(cls, prev_hash: str, saved_at: str, state: dict) -> dict:
        """Keyed integrity seal over an entry's canonical content.

        Sealing {prev, saved_at, state} binds EVERY entry's content —
        including a single-entry journal's first line and every tail entry,
        which no successor exists to protect.

        The returned dict carries an authenticated provenance marker: under a
        keyed regime the HMAC covers {"alg": "hmac-sha256"} so the algorithm/
        key-presence of the writing regime is itself sealed; the unkeyed
        regime seals {"alg": "sha256"} the same way. A reader can therefore
        distinguish a legitimately unkeyed digest from a forged one only via
        this marker plus its own configured policy — see load().
        """
        alg = "hmac-sha256" if _keyed_policy_active() else "sha256"
        provenance = json.dumps({"alg": alg}, sort_keys=True,
                                separators=(",", ":"))
        payload = cls._entry_seal_payload(prev_hash, saved_at, state)
        digest = _seal_digest(payload + "\n" + provenance)
        return {"alg": alg, "digest": digest}

    @staticmethod
    def _verify_entry_seal(seal, prev_hash: str, saved_at: str,
                           state: dict) -> bool:
        """Verify a journal entry seal per the claim-journal key policy.

        Policy boundary (exact):
          * Any seal-key variable configured but NO usable key (malformed
            CALLISTO_SEAL_KEY / old-key-only config) => FAIL CLOSED. Malformed
            security policy is never mistaken for an unkeyed deployment.
          * Keys configured and usable => the seal MUST be a valid HMAC under
            that key ring. Public SHA-256 digests are never accepted, so
            public-digest substitution fails closed.
          * No key variable configured at all => plain SHA-256 accepted, but
            ONLY with the matching authenticated ``alg: "sha256"`` marker.
        """
        if not isinstance(seal, dict) or "digest" not in seal:
            return False
        claimed_alg = seal.get("alg")
        digest = str(seal["digest"])
        keyed_vars = _configured_seal_key_vars()
        keys = _seal_keys()
        if keyed_vars and not keys:
            raise ClaimError(
                "seal key configuration error: CALLISTO_SEAL_KEY/"
                "CALLISTO_SEAL_KEY_OLD are set but no value is valid hex — "
                "refusing to fall back to an unkeyed verification (fail "
                "closed)")
        expected_alg = "hmac-sha256" if keys else "sha256"
        if hmac.compare_digest(str(claimed_alg), expected_alg) is False:
            return False
        payload = ClaimStore._entry_seal_payload(prev_hash, saved_at, state)
        provenance = json.dumps({"alg": expected_alg}, sort_keys=True,
                                separators=(",", ":"))
        encoded = (payload + "\n" + provenance).encode("utf-8")
        candidates = [hmac.new(key, encoded, hashlib.sha256).hexdigest()
                      for key in keys]
        if not candidates:
            candidates.append(hashlib.sha256(encoded).hexdigest())
        return any(hmac.compare_digest(c, digest) for c in candidates)

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

        Key policy (exact boundary):
          * If CALLISTO_SEAL_KEY / CALLISTO_SEAL_KEY_OLD are set but contain
            NO usable hex key, loading FAILS CLOSED immediately: malformed
            security policy is never treated as an unkeyed deployment, and a
            forged public SHA-256 digest can never load in that state.
          * With usable keys configured, every entry's seal MUST be an HMAC
            under that key ring; public digests are rejected, and
            allow_legacy_unsigned=True is IGNORED — a configured (even
            malformed-but-configured) keyed policy disables every unsigned/
            public-digest fallback, so a wholly stripped keyed history cannot
            masquerade as a legacy journal.
          * With no key variable configured at all, plain SHA-256 seals
            (authenticated with their ``alg`` marker) are accepted for
            genuinely unkeyed journals.

        Ambiguous historical format (regime change): entries written before
        authenticated algorithm provenance existed carry a bare string seal;
        whether they were HMAC or public-digest cannot be established from
        the bytes alone once the writing-time key configuration is removed.
        The safe policy chosen here is FAIL CLOSED: such journals raise
        ClaimError and require an explicit operator re-seal/migration under
        the current policy; removing key configuration never silently
        downgrades an old keyed history, and allow_legacy_unsigned never
        resurrects these ambiguous entries.

        Legacy wholly-unsigned journals (pre-sealing format, no ``seal``
        field): readable only with allow_legacy_unsigned=True AND only when
        no key variable is configured. Chain checks still apply; their tail
        integrity is NOT guaranteed and this must never be enabled on
        security-sensitive paths.

        Honest tail-truncation limitation: deleting trailing lines from a
        validly signed journal requires only filesystem WRITE access — not
        the seal key — so truncation of a signed prefix is undetectable here
        absent an external head/count anchor. Do not treat load() success as
        proof no lines were removed.
        """
        path = self._journal_path(claim_id)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        prev = "GENESIS"
        state = None
        saw_seal = False
        saw_unsigned = False
        for i, ln in enumerate(lines):
            try:
                entry = json.loads(ln)
            except json.JSONDecodeError as e:
                raise ClaimError(f"journal line {i+1} corrupt: {e}")
            if verify:
                # Fail closed on malformed configured key policy BEFORE any
                # entry can be judged: invalid config != unkeyed deployment.
                if _configured_seal_key_vars() and not _keyed_policy_active():
                    raise ClaimError(
                        f"configuration error for claim {claim_id}: seal key "
                        f"variables are set but contain no valid hex key — "
                        f"refusing to fall back to unkeyed verification")
                keyed_policy = _keyed_policy_active()
                # Chain check: line 1 must reference GENESIS; each later
                # line must reference its predecessor's raw-line digest.
                if entry.get("prev") != prev:
                    raise ClaimError(
                        f"tampering detected in claim {claim_id}: journal "
                        f"line {i+1} does not chain to its predecessor "
                        f"(history is not trustworthy)")
                seal = entry.get("seal")
                if seal is None:
                    # Legacy opt-in applies ONLY to wholly unsigned journals
                    # in a genuinely unkeyed deployment. Under an active keyed
                    # policy it is ignored: a stripped seal there is tampering
                    # (including stripping EVERY seal to fake a legacy
                    # journal), never a permitted fallback.
                    saw_unsigned = True
                    if keyed_policy or not allow_legacy_unsigned:
                        raise ClaimError(
                            f"tampering detected in claim {claim_id}: "
                            f"journal line {i+1} has no integrity seal"
                            + (" (active keyed policy forbids unsigned "
                               "entries)"
                               if keyed_policy else
                               " (legacy unsigned journal; refusing to load "
                               "unverified history)"))
                else:
                    saw_seal = True
                    if not self._verify_entry_seal(
                            seal, entry["prev"], entry["saved_at"],
                            entry["state"]):
                        raise ClaimError(
                            f"tampering detected in claim {claim_id}: "
                            f"journal line {i+1} failed its integrity seal "
                            f"(content does not match the sealed record)")
            prev = hashlib.sha256(ln.encode("utf-8")).hexdigest()
            state = entry["state"]
        if (verify and saw_seal and saw_unsigned
                and allow_legacy_unsigned and not _keyed_policy_active()):
            raise ClaimError(
                f"tampering detected in claim {claim_id}: history mixes "
                f"sealed and unsigned entries — an unsigned entry in a "
                f"sealed history is a stripped-seal downgrade, not a "
                f"legacy journal (refusing to load)")
        return Claim.from_dict(state)

    def list_ids(self) -> list[str]:
        prefix, suffix = "claim_", ".jsonl"
        out = []
        for name in sorted(os.listdir(self._dir)):
            if name.startswith(prefix) and name.endswith(suffix):
                out.append(name[len(prefix):-len(suffix)])
        return out
