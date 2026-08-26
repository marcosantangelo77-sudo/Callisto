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


class SealPolicyError(ClaimError):
    """Raised when the seal-key configuration itself is malformed.

    A malformed security policy must never be silently reinterpreted as a
    weaker one (fail closed).
    """


# Seal REGIME — an explicit external policy anchor, never inferred from
# attacker-writable journal data.
#
#   KEYED       HMAC-SHA256 under CALLISTO_SEAL_KEY (+ optional rotation key
#               CALLISTO_SEAL_KEY_OLD). Default when a valid current key is
#               configured. New entries are sealed with the CURRENT key; old
#               keys verify only. Public/unsigned fallbacks are rejected,
#               including allow_legacy_unsigned=True.
#   UNKEYED     Public SHA-256 checksums, chosen ONLY via an explicit
#               compatibility opt-in (CALLISTO_SEAL_POLICY=unkeyed or
#               ClaimStore(..., seal_policy="unkeyed")). Provides
#               tamper-EVIDENCE against third parties, not authenticity:
#               anyone who can write the journal can recompute the digest.
#   UNSPECIFIED No policy declared and no usable key configured: FAIL CLOSED
#               on both save and load. Silence is never read as "unkeyed".
#
# Honest limits (documented, not fixable inside the journal format):
#   * The regime marker inside an entry is covered by the seal but is not,
#     by itself, a provenance anchor: an attacker who can rewrite the file
#     and controls the external configuration can re-seal under any regime.
#     The journal defends only against attackers who cannot alter the
#     external policy/configuration.
#   * Tail truncation: deleting trailing signed lines requires only
#     filesystem WRITE access, not the key; absent an external head/count
#     anchor, load() success proves nothing about removed tail lines.


def _parse_seal_policy(
        *, env_policy=None, ctor_policy=None) -> tuple[str, list[bytes],
                                                       list[bytes]]:
    """Single strict policy parser used by BOTH save and load.

    Returns ``(regime, current_keys, old_keys)`` where regime is one of
    ``"keyed"``, ``"unkeyed"`` or raises SealPolicyError. Every nonblank
    configured token must be valid hex; no malformed token is ever dropped.

    Raises SealPolicyError on:
      * unknown/unreadable policy values;
      * any malformed CALLISTO_SEAL_KEY / CALLISTO_SEAL_KEY_OLD token;
      * a keyed declaration without a valid current key (old keys are
        verification-only and never authorize new writes);
      * contradictory declarations (explicit unkeyed opt-in while a seal-key
        variable is also set).
    """
    if (ctor_policy is not None and env_policy
            and str(ctor_policy).strip().lower()
            != str(env_policy).strip().lower()):
        raise SealPolicyError(
            f"conflicting seal policy: constructor {ctor_policy!r} vs "
            f"environment {env_policy!r} — configure exactly one")
    raw = ctor_policy if ctor_policy is not None else env_policy
    if raw is not None and str(raw).strip().lower() not in ("keyed", "unkeyed"):
        raise SealPolicyError(f"unknown seal policy {raw!r}")

    cur_raw = os.getenv("CALLISTO_SEAL_KEY", "").strip()
    old_raws = [t.strip()
                for t in os.getenv("CALLISTO_SEAL_KEY_OLD", "").split(",")
                if t.strip()]

    def _hex(tok: str) -> bytes:
        try:
            k = bytes.fromhex(tok)
        except ValueError:
            raise SealPolicyError(
                f"malformed seal key token ({tok[:8]!r}...): must be hex — "
                f"refusing to ignore or downgrade it") from None
        if not k:
            raise SealPolicyError("malformed seal key token: empty key")
        return k

    current = [_hex(cur_raw)] if cur_raw else []
    olds = [_hex(t) for t in old_raws]

    # Explicit declaration wins and must be self-consistent.
    if raw is not None:
        regime = str(raw).strip().lower()
        if regime == "keyed" and not current:
            raise SealPolicyError(
                "seal policy 'keyed' requires a valid CALLISTO_SEAL_KEY "
                "(old keys are verification-only)")
        if regime == "unkeyed" and (current or olds):
            raise SealPolicyError(
                "seal policy 'unkeyed' conflicts with configured seal-key "
                "variables — remove them or choose the keyed regime")
        return regime, current, olds

    # No explicit declaration: presence of a valid current key selects the
    # keyed regime; anything else fails closed.
    if current:
        return "keyed", current, olds
    if cur_raw or os.getenv("CALLISTO_SEAL_KEY_OLD", "").strip():
        raise SealPolicyError(
            "malformed seal key configuration: seal-key variables are set "
            "but contain no valid current key — refusing to fall back to an "
            "unkeyed regime (fail closed)")
    raise SealPolicyError(
        "no seal policy configured: set CALLISTO_SEAL_POLICY=unkeyed to "
        "explicitly accept public-checksum seals, or configure a valid "
        "CALLISTO_SEAL_KEY for authenticated seals (fail closed)")


def _resolve_seal_regime(ctor_policy=None) -> tuple[str, list[bytes], list[bytes]]:
    return _parse_seal_policy(env_policy=os.getenv("CALLISTO_SEAL_POLICY"),
                              ctor_policy=ctor_policy)



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

    def __init__(self, directory: str, *, seal_policy: Optional[str] = None):
        """``seal_policy``: explicit "keyed"/"unkeyed" regime override.

        Omitted => environment (CALLISTO_SEAL_POLICY) decides; with neither,
        construction succeeds but every save/load fails closed until a
        policy is declared. See the seal-regime block above this class.
        """
        self._dir = directory
        self._ctor_seal_policy = seal_policy
        os.makedirs(self._dir, exist_ok=True)

    def _policy(self) -> tuple[str, list[bytes], list[bytes]]:
        return _resolve_seal_regime(ctor_policy=self._ctor_seal_policy)

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
    def _entry_seal(cls, prev_hash: str, saved_at: str, state: dict,
                    regime: str, keys: list[bytes]) -> dict:
        """Seal an entry's canonical content under an ALREADY-RESOLVED policy.

        Sealing {prev, saved_at, state} binds EVERY entry's content —
        including a single-entry journal's first line and every tail entry.
        The authenticated provenance marker {alg} is covered by the digest;
        it records the writing regime but is NOT, by itself, a provenance
        anchor (see the honest-limits note in the module policy block).
        """
        alg = "hmac-sha256" if regime == "keyed" else "sha256"
        payload = cls._entry_seal_payload(prev_hash, saved_at, state)
        encoded = (payload + "\n"
                   + json.dumps({"alg": alg}, sort_keys=True,
                                separators=(",", ":"))).encode("utf-8")
        if regime == "keyed":
            # Sign under the CURRENT key only; old keys verify history.
            digest = hmac.new(keys[0], encoded, hashlib.sha256).hexdigest()
        else:
            digest = hashlib.sha256(encoded).hexdigest()
        return {"alg": alg, "digest": digest}

    @staticmethod
    def _verify_entry_seal(seal, prev_hash: str, saved_at: str,
                           state: dict, regime: str,
                           keys: list[bytes]) -> bool:
        """Strict envelope check + digest verification for the given regime."""
        if not isinstance(seal, dict):
            return False
        if set(seal) - {"alg", "digest"}:
            return False
        claimed_alg = seal.get("alg")
        raw_digest = seal.get("digest")
        if not isinstance(raw_digest, (str, int)):
            return False
        digest = str(raw_digest)
        expected_alg = "hmac-sha256" if regime == "keyed" else "sha256"
        if not isinstance(claimed_alg, str) or claimed_alg != expected_alg:
            return False
        if not isinstance(prev_hash, str) or not isinstance(saved_at, str) \
                or not isinstance(state, dict):
            return False
        payload = ClaimStore._entry_seal_payload(prev_hash, saved_at, state)
        encoded = (payload + "\n"
                   + json.dumps({"alg": expected_alg}, sort_keys=True,
                                separators=(",", ":"))).encode("utf-8")
        candidates = ([hmac.new(k, encoded, hashlib.sha256).hexdigest()
                       for k in keys] if regime == "keyed"
                      else [hashlib.sha256(encoded).hexdigest()])
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
        # Resolve the seal policy BEFORE any write: malformed or missing
        # configuration raises here and nothing is appended (fail closed).
        regime, current_keys, _old_keys = self._policy()
        saved_at = _now_iso()
        state = claim.to_dict()
        entry = {"prev": prev_hash, "saved_at": saved_at,
                 "state": state,
                 "seal": self._entry_seal(prev_hash, saved_at, state,
                                          regime, current_keys)}
        blob = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(blob + "\n")
        return count + 1

    def load(self, claim_id: str, verify: bool = True,
             allow_legacy_unsigned: bool = False) -> Optional[Claim]:
        """Replay the journal to latest verified state.

        Policy (exact boundary — see the seal-regime block at module top):
          * The SAME strict parser used by save() resolves the regime first;
            any malformed configuration raises SealPolicyError before any
            entry is judged.
          * keyed: every seal MUST be an HMAC under the current/old key ring.
            Public digests, unsigned entries and allow_legacy_unsigned are
            all rejected. Key REMOVAL cannot downgrade a keyed history: the
            journal alone cannot prove its own regime, so with no usable key
            configured it simply becomes unloadable (fail closed) until an
            explicit policy or migration is declared.
          * unkeyed (explicit opt-in only): public SHA-256 seals accepted.
            This is a tamper-EVIDENCE checksum, not authenticity.
          * UNSPECIFIED (no policy, no keys): fail closed on both save/load.

        Legacy handling:
          * Bare-string historical seals (ambiguous HMAC vs public digest)
            always raise ClaimError; use migrate_legacy_journal().
          * Wholly-unsigned journals: readable ONLY via allow_legacy_unsigned=True
            in an explicitly UNKEYED deployment; chain checks still apply.

        Honest limitation: a filesystem writer can delete a validly signed
        tail without the key; absent an external head/count anchor, load()
        success does not prove no lines were removed.
        """
        path = self._journal_path(claim_id)
        if not os.path.exists(path):
            return None
        # Strict shared parser: malformed config fails closed BEFORE reading.
        regime, current_keys, old_keys = self._policy()
        keys = list(current_keys) + [k for k in old_keys if k not in current_keys]
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        prev = "GENESIS"
        state = None
        saw_seal = False
        saw_unsigned = False

        def _fail(msg: str):
            raise ClaimError(f"{msg} in claim {claim_id} "
                             f"(journal line {i+1})")

        for i, ln in enumerate(lines):
            try:
                entry = json.loads(ln)
            except json.JSONDecodeError as e:
                raise ClaimError(f"journal line {i+1} corrupt: {e}") from e
            if verify:
                if not isinstance(entry, dict):
                    _fail(f"malformed journal line {i+1}: expected object")
                if entry.get("prev") != prev:
                    _fail("tampering detected: entry does not chain to its "
                          "predecessor (history is not trustworthy)")
                seal = entry.get("seal")
                if seal is None and "seal" in entry:
                    # Present-but-null is MALFORMED, never "missing": it
                    # must not pass as a legacy unsigned entry.
                    _fail("malformed seal envelope: seal is null (distinct "
                          "from an absent legacy seal)")
                if seal is None:
                    saw_unsigned = True
                    if regime == "keyed":
                        _fail("tampering detected: active keyed policy "
                              "forbids unsigned entries")
                    if not allow_legacy_unsigned:
                        _fail("tampering detected: entry has no integrity "
                              "seal (legacy unsigned journal; refusing to "
                              "load unverified history)")
                elif isinstance(seal, dict):
                    # Present-but-malformed envelope values are ClaimErrors,
                    # never silent acceptance.
                    try:
                        ok = self._verify_entry_seal(
                            seal, entry["prev"], entry["saved_at"],
                            entry["state"], regime, keys)
                    except (TypeError, KeyError, AttributeError, ValueError) \
                            as e:
                        raise ClaimError(
                            f"malformed seal envelope in claim {claim_id} "
                            f"(journal line {i+1}): {e!r}") from e
                    if not ok:
                        _fail("tampering detected: entry failed its "
                              "integrity seal (content does not match the "
                              "sealed record)")
                    saw_seal = True
                else:
                    _fail("unsupported legacy bare-string seal format; use "
                          "migrate_legacy_journal() to re-seal explicitly")
            if not isinstance(entry.get("saved_at"), str) \
                    or not isinstance(entry.get("state"), dict):
                raise ClaimError(
                    f"malformed journal entry shape in claim {claim_id} "
                    f"(journal line {i+1})")
            prev = hashlib.sha256(ln.encode("utf-8")).hexdigest()
            state = entry["state"]
        if verify and saw_seal and saw_unsigned:
            _fail_tail = (
                "tampering detected: history mixes sealed and unsigned "
                "entries — an unsigned entry in a sealed history is a "
                "stripped-seal downgrade, not a legacy journal")
            raise ClaimError(f"{_fail_tail} in claim {claim_id}")
        return Claim.from_dict(state)

    # ── operator-attested legacy migration ───────────────────────────────

    def migrate_legacy_journal(self, claim_id: str,
                               *, attest_unverified: bool = False) -> int:
        """Operator-attested re-seal of an unloadable legacy history.

        Rewrites the journal ATOMICALLY (temp file + os.replace): every entry
        that can be verified under the current policy keeps its verified
        state; every unverifiable entry (bare-string seal, stripped seal) is
        carried over ONLY when ``attest_unverified=True``, marking each such
        line ``migrated_unverified: true`` so the unverified provenance stays
        visible forever. Never silently appends a new-format envelope onto
        unsupported history, and never treats unverified data as
        authenticated.

        The result loads under the CURRENT strict policy. Chain links are
        rebuilt from the rewritten raw lines. Returns the new entry count.
        """
        path = self._journal_path(claim_id)
        if not os.path.exists(path):
            raise ClaimError(f"no journal exists for claim {claim_id}")
        regime, current_keys, old_keys = self._policy()
        keys = list(current_keys) + [k for k in old_keys if k not in current_keys]
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        prev = "GENESIS"
        out_lines = []
        attested_any = False
        for i, ln in enumerate(lines):
            try:
                entry = json.loads(ln)
            except json.JSONDecodeError as e:
                raise ClaimError(
                    f"cannot migrate claim {claim_id}: line {i+1} is not "
                    f"valid JSON ({e})") from e
            if not isinstance(entry, dict):
                raise ClaimError(
                    f"cannot migrate claim {claim_id}: line {i+1} is not an "
                    f"object")
            state = entry.get("state")
            saved_at = entry.get("saved_at")
            if not isinstance(state, dict) or not isinstance(saved_at, str):
                raise ClaimError(
                    f"cannot migrate claim {claim_id}: line {i+1} has "
                    f"malformed state/saved_at")
            verified = False
            seal = entry.get("seal")
            if isinstance(seal, dict):
                try:
                    verified = self._verify_entry_seal(
                        seal, entry["prev"], saved_at, state,
                        regime, keys)
                except (TypeError, KeyError, AttributeError, ValueError):
                    verified = False
            migrated_unverified = False
            if not verified:
                if not attest_unverified:
                    raise ClaimError(
                        f"cannot migrate claim {claim_id}: line {i+1} "
                        f"cannot be verified under the current policy — "
                        f"re-run with attest_unverified=True to accept it "
                        f"as explicitly UNVERIFIED history")
                migrated_unverified = True
                attested_any = True
            new_entry = {"prev": prev, "saved_at": saved_at,
                         "state": state}
            if migrated_unverified:
                new_entry["migrated_unverified"] = True
            payload = ClaimStore._entry_seal_payload(prev, saved_at, state)
            blob = json.dumps(new_entry, sort_keys=True, ensure_ascii=False)
            sealed = self._entry_seal(prev, saved_at, state,
                                      regime, current_keys)
            out_blob = json.dumps({**new_entry, "seal": sealed},
                                  sort_keys=True, ensure_ascii=False)
            out_lines.append(out_blob)
            prev = hashlib.sha256(out_blob.encode("utf-8")).hexdigest()
        tmp = path + ".migrate.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if attested_any:
            _log.warning(
                "[claim journal] %s: migrated %d-entry history with "
                "operator-attested UNVERIFIED entries under %s regime",
                claim_id, len(out_lines), regime)
        return len(out_lines)

    def list_ids(self) -> list[str]:
        prefix, suffix = "claim_", ".jsonl"
        out = []
        for name in sorted(os.listdir(self._dir)):
            if name.startswith(prefix) and name.endswith(suffix):
                out.append(name[len(prefix):-len(suffix)])
        return out
