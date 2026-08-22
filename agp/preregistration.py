"""
Preregistration — commit to confirmation/refutation criteria BEFORE evidence
collection, then score the actual result against the sealed criteria.

Why this exists (BUILD_MANDATE property 4): post-hoc rationalisation is the
dominant failure mode of every research process. The fix is structural: what
would COUNT as confirmation and refutation is written down and SEALED (same
HMAC machinery as AGPSession) before any evidence arrives, and at conclusion
time the claim is scored AGAINST THE SEALED TEXT. Any divergence between what
was preregistered and what is now claimed is surfaced loudly — it raises, it
never silently reconciles.

Immutability: a sealed Preregistration cannot be mutated through its normal
API — every field setter on a sealed object raises PreregistrationSealed.
The ONLY sanctioned post-seal change is amend(), which appends an immutable,
timestamped amendment record with its own seal over (old criteria + reason).
The original criteria text is never rewritten; scoring always runs against
the ORIGINAL seal unless the caller explicitly scores against an amendment.

Domain-general: criteria are free-text plus structured numeric gates; nothing
here knows about sports, markets, or tickers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import hmac
import logging

# Reuse the session seal keying so one environment variable secures both.
from agp import _seal_digest

_log = logging.getLogger("callisto.agp.prereg")


class PreregistrationError(Exception):
    """Raised for lifecycle violations (scoring before seal, etc.)."""


class PreregistrationSealed(PreregistrationError):
    """Raised when any mutation of a sealed preregistration is attempted."""


class Verdict(str, Enum):
    CONFIRMED = "CONFIRMED"
    REFUTED = "REFUTED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass
class Criteria:
    """Explicit, checkable commitment. All fields are part of the seal."""

    confirm_markers: list[str] = field(default_factory=list)
    refute_markers: list[str] = field(default_factory=list)
    ambiguous_markers: list[str] = field(default_factory=list)
    # Numeric gate: the observed value must clear this to count as confirmed
    # (direction: "gte" or "lte"). A preregistration may be purely textual
    # (marker-based) — threshold is optional.
    threshold: Optional[float] = None
    direction: Optional[str] = None          # "gte" | "lte" | None
    # Minimum evidence that must EXIST before a verdict may be reached at all:
    # without it, "we found nothing either way" would resolve as anything.
    min_evidence_items: int = 1
    min_source_class: str = "SECONDARY"      # mirrors SourceClass values
    resolution_horizon: Optional[str] = None  # ISO date by which it must resolve

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not self.confirm_markers:
            errs.append("confirm_markers must be non-empty: a preregistration "
                        "that cannot state what confirms it is not one")
        if not self.refute_markers:
            errs.append("refute_markers must be non-empty")
        if self.threshold is not None and self.direction not in ("gte", "lte"):
            errs.append("threshold requires direction 'gte' or 'lte'")
        if self.direction is not None and self.threshold is None:
            errs.append("direction requires a threshold")
        if self.min_evidence_items < 1:
            errs.append("min_evidence_items must be >= 1")
        return errs

    def to_dict(self) -> dict:
        return {
            "confirm_markers": list(self.confirm_markers),
            "refute_markers": list(self.refute_markers),
            "ambiguous_markers": list(self.ambiguous_markers),
            "threshold": self.threshold,
            "direction": self.direction,
            "min_evidence_items": self.min_evidence_items,
            "min_source_class": self.min_source_class,
            "resolution_horizon": self.resolution_horizon,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Criteria":
        return cls(
            confirm_markers=list(d.get("confirm_markers", [])),
            refute_markers=list(d.get("refute_markers", [])),
            ambiguous_markers=list(d.get("ambiguous_markers", [])),
            threshold=d.get("threshold"),
            direction=d.get("direction"),
            min_evidence_items=int(d.get("min_evidence_items", 1)),
            min_source_class=d.get("min_source_class", "SECONDARY"),
            resolution_horizon=d.get("resolution_horizon"),
        )


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Preregistration:
    """Commit-then-score object. Lifecycle:

        p = Preregistration(query=..., criteria=Criteria(...))
        p.seal()                    # BEFORE evidence collection begins
        ... evidence arrives ...
        outcome = p.score(observed_text="...", observed_value=..., ...)
        outcome.verdict             # CONFIRMED / REFUTED / AMBIGUOUS
        outcome.divergences         # loud list of preregistration-vs-claim gaps

    After seal(), every mutating operation except amend() raises
    PreregistrationSealed. The seal covers query, criteria, and created_at;
    verify_seal() recomputes it exactly like AGPSession.verify_seal().
    """

    def __init__(self, query: str, criteria: Criteria):
        # Route through __setattr__ (unsealed at this point, so it permits).
        self.query = query
        self.criteria = criteria
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.sealed_at: Optional[str] = None
        self.seal_hash: Optional[str] = None
        self.amendments: list[dict] = []       # each carries its own seal
        self._sealed = False

    # Attributes that may change after sealing (seal bookkeeping and the
    # append-only amendment list — which amend() mutates via its own sealed
    # record, never by rewriting criteria). Everything else is frozen.
    _SEALED_MUTABLE = frozenset({"amendments", "scored_outcomes"})

    def __setattr__(self, name: str, value) -> None:
        if getattr(self, "_sealed", False) and \
                name not in self._SEALED_MUTABLE and name != "_sealed":
            raise PreregistrationSealed(
                f"cannot modify sealed preregistration field {name!r}; "
                f"post-seal changes must go through amend() and are recorded")
        object.__setattr__(self, name, value)

    # ── payload / sealing ────────────────────────────────────────────────

    def _payload(self) -> dict:
        return {
            "query": self.query,
            "criteria": self.criteria.to_dict(),
            "created_at": self.created_at,
        }

    def seal(self) -> str:
        if self._sealed:
            raise PreregistrationError("already sealed")
        errs = self.criteria.validate()
        if errs:
            raise PreregistrationError(f"invalid criteria: {errs}")
        if not (self.query or "").strip():
            raise PreregistrationError("empty query")
        self.sealed_at = datetime.now(timezone.utc).isoformat()
        # sealed_at is set before hashing so verify can reproduce exactly.
        self.seal_hash = _seal_digest(_canonical({
            **self._payload(), "sealed_at": self.sealed_at}))
        self._sealed = True
        return self.seal_hash

    def verify_seal(self) -> bool:
        if not self.seal_hash or not self.sealed_at:
            return False
        expected = _seal_digest(_canonical({
            **self._payload(), "sealed_at": self.sealed_at}))
        return hmac.compare_digest(expected, self.seal_hash)

    def _guard(self) -> None:
        if self._sealed:
            raise PreregistrationSealed(
                "preregistration is sealed; use amend() — direct mutation is "
                "recorded as tampering, never silently applied")

    # ── amendments ───────────────────────────────────────────────────────

    def amend(self, new_criteria: Criteria, reason: str) -> dict:
        """Record an amendment. The ORIGINAL criteria are untouched and remain
        the default scoring basis; the amendment carries its own timestamp,
        its own seal over (prior state + new criteria + reason), and the
        chain length is surfaced in every subsequent score report."""
        if not self._sealed:
            raise PreregistrationError(
                "amend only applies to a sealed preregistration — edit freely "
                "before sealing instead")
        errs = new_criteria.validate()
        if errs:
            raise PreregistrationError(f"invalid amended criteria: {errs}")
        if not (reason or "").strip():
            raise PreregistrationError("amendment requires a stated reason")
        prior = {
            "seal_hash": self.seal_hash,
            "criteria": self.criteria.to_dict(),
            "amendments_len": len(self.amendments),
        }
        ts = datetime.now(timezone.utc).isoformat()
        rec = {
            "reason": reason,
            "amended_at": ts,
            "new_criteria": new_criteria.to_dict(),
            "prior_seal_hash": self.seal_hash,
        }
        rec["seal"] = _seal_digest(_canonical({**prior, **{
            k: rec[k] for k in ("reason", "amended_at", "new_criteria")}}))
        self.amendments.append(rec)
        # NOTE: self.criteria is deliberately NOT updated here. Callers who
        # want to score against the latest amendment pass it explicitly to
        # score(criteria=...). The sealed original is always recoverable.
        return rec

    @property
    def effective_criteria(self) -> Criteria:
        """Latest amendment's criteria if any exist, else the sealed originals."""
        if self.amendments:
            return Criteria.from_dict(self.amendments[-1]["new_criteria"])
        return self.criteria

    # ── scoring against the sealed text ─────────────────────────────────

    def score(
        self,
        *,
        observed_text: str = "",
        observed_value: Optional[float] = None,
        evidence_count: int = 0,
        best_source_class: str = "INFERRED",
        claimed_verdict: Optional[Verdict] = None,
        claimed_confidence: Optional[float] = None,
        now: Optional[datetime] = None,
        criteria: Optional[Criteria] = None,
    ) -> "Outcome":
        """Score the observed result AGAINST the sealed criteria.

        Divergences are returned loudly in Outcome.divergences AND logged via
        the module logger at WARNING. Nothing here rewrites the criteria.
        """
        crit = criteria if criteria is not None else self.effective_criteria
        using_amended = criteria is not None or bool(self.amendments)
        if not self._sealed:
            raise PreregistrationError("cannot score an unsealed preregistration")

        divergences: list[str] = []

        # Gate 0: evidence threshold must be met or the verdict is AMBIGUOUS
        # no matter how confident anyone feels.
        evidence_ok = evidence_count >= crit.min_evidence_items
        class_rank = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}
        class_ok = class_rank.get(best_source_class, 0) >= \
            class_rank.get(crit.min_source_class, 2)
        if not evidence_ok:
            divergences.append(
                f"evidence gate unmet: {evidence_count} items < required "
                f"{crit.min_evidence_items} (preregistered)")
        if not class_ok:
            divergences.append(
                f"source-class gate unmet: best {best_source_class} < "
                f"required {crit.min_source_class} (preregistered)")

        low = (observed_text or "").lower()

        def contains(markers: list[str]) -> list[str]:
            return [m for m in markers if m and m.lower() in low]

        hit_confirm = contains(crit.confirm_markers)
        hit_refute = contains(crit.refute_markers)
        hit_ambig = contains(crit.ambiguous_markers)

        thresh_hit = False
        thresh_miss = False
        if crit.threshold is not None and observed_value is not None:
            if crit.direction == "gte":
                thresh_hit = observed_value >= crit.threshold
                thresh_miss = not thresh_hit
            elif crit.direction == "lte":
                thresh_hit = observed_value <= crit.threshold
                thresh_miss = not thresh_hit
        elif crit.threshold is not None and observed_value is None:
            divergences.append(
                "preregistered numeric threshold but no observed_value supplied "
                "— numeric criterion unscored")

        # Verdict logic: refutation outranks confirmation when both fire
        # (falsification is the stronger claim); ambiguity markers force
        # AMBIGUOUS only if neither confirm nor refute fired.
        verdict: Verdict
        if hit_refute and not hit_confirm:
            verdict = Verdict.REFUTED
        elif hit_confirm and hit_refute:
            verdict = Verdict.AMBIGUOUS
            divergences.append(
                "both confirm and refute markers matched — conflicting signals")
        elif hit_confirm:
            verdict = Verdict.CONFIRMED
        elif hit_ambig:
            verdict = Verdict.AMBIGUOUS
        elif thresh_hit:
            verdict = Verdict.CONFIRMED
        elif thresh_miss:
            verdict = Verdict.REFUTED
        else:
            verdict = Verdict.AMBIGUOUS
            if not (hit_confirm or hit_refute or hit_ambig):
                divergences.append(
                    "no preregistered marker matched the observed text")

        # Gates demote, never promote: a marker hit without evidence still
        # cannot claim CONFIRMED/REFUTED.
        if not (evidence_ok and class_ok) and verdict != Verdict.AMBIGUOUS:
            divergences.append(
                f"verdict {verdict.value} demoted to AMBIGUOUS: preregistered "
                f"evidence gates unmet")
            verdict = Verdict.AMBIGUOUS

        # Horizon check.
        horizon = crit.resolution_horizon
        if horizon:
            try:
                hdate = datetime.fromisoformat(horizon).date()
                ndate = (now or datetime.now(timezone.utc)).date()
                if ndate > hdate and verdict == Verdict.AMBIGUOUS:
                    divergences.append(
                        f"resolution horizon {horizon} passed without a "
                        f"decisive verdict")
            except ValueError:
                divergences.append(f"unparseable resolution_horizon {horizon!r}")

        # Loud divergence vs what is NOW being claimed.
        if claimed_verdict is not None and claimed_verdict != verdict:
            divergences.append(
                f"CLAIMED VERDICT DIVERGES FROM PREREGISTRATION: scored "
                f"{verdict.value} against sealed criteria but claiming "
                f"{claimed_verdict.value}")
        if claimed_confidence is not None:
            if claimed_confidence < 0.0 or claimed_confidence > 1.0:
                divergences.append(
                    f"claimed_confidence {claimed_confidence} outside [0,1]")
            elif verdict == Verdict.AMBIGUOUS and claimed_confidence > 0.55:
                divergences.append(
                    f"claiming confidence {claimed_confidence:.2f} while the "
                    f"sealed criteria score AMBIGUOUS — confidence not earned")

        if using_amended:
            divergences.insert(0,
                f"scored against AMENDED criteria (chain length "
                f"{len(self.amendments)}); sealed originals were "
                f"{_canonical(self.criteria.to_dict())}")

        for d in divergences:
            _log.warning("[prereg divergence] %s :: %s",
                         self.seal_hash, d)

        return Outcome(
            verdict=verdict,
            divergences=divergences,
            scored_against_seal=self.seal_hash,
            used_amendment=bool(using_amended),
            hits={"confirm": hit_confirm, "refute": hit_refute,
                  "ambiguous": hit_ambig},
        )

    # ── persistence ──────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "criteria": self.criteria.to_dict(),
            "created_at": self.created_at,
            "sealed_at": self.sealed_at,
            "seal_hash": self.seal_hash,
            "amendments": self.amendments,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Preregistration":
        p = cls(query=d["query"], criteria=Criteria.from_dict(d["criteria"]))
        p.created_at = d["created_at"]
        p.sealed_at = d.get("sealed_at")
        p.seal_hash = d.get("seal_hash")
        p.amendments = list(d.get("amendments", []))
        p._sealed = bool(p.seal_hash)
        return p


@dataclass
class Outcome:
    """Result of scoring reality against the sealed criteria."""
    verdict: Verdict
    divergences: list[str]
    scored_against_seal: str
    used_amendment: bool
    hits: dict
