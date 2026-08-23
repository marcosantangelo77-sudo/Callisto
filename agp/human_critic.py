"""
THE HUMAN CRITIC — the owner as a scored adversary (BUILD_MANDATE §1 property 4,
NEXT.md §5 dissent logging).

The highest-value human input is not "approve this conclusion" — it is
"I disagree with this claim, and here is why." This module makes that dissent
a first-class objection with the SAME structure as a model critic's: text,
severity, attack axis, attacked claim. It enters the SAME AdversaryLedger, so
per-critic calibration works identically for a human and a model — the owner
is simply another critic, keyed ``human:<name>`` in calibration_by_model.

Three structural properties:

1. ASYMMETRY, identical to the Adversary's. A human objection may LOWER
   confidence or VETO a seal. Human AGREEMENT may never raise a score above
   what provenance permits — there is no code path here that increases any
   score. The owner is a critic, not an override of the evidence rules.

2. SCORED TRACK RECORD. Objections are tracked SUSTAINED/OVERRULED like any
   model's, and once the claim resolves, scored RIGHT/WRONG against reality.
   calibration() reports whether the OWNER is calibrated — too harsh, too
   soft, or honest — overall and PER DOMAIN, exactly as calibration_by_model
   does for models. If he is systematically wrong in one domain, the system
   can show him that instead of silently absorbing his errors.

3. OVERRULING IS A DECISION WITH A REASON. When the human overrules the
   SYSTEM (forces a seal past a sustained model objection, reopens a refused
   seal, dismisses a machine objection), the act appends a HumanOverride
   record — who, what, why, at what confidence — to an append-only JSONL.
   A silent edit would corrupt the very calibration record this module
   exists to build.

Domain-general throughout. Storage mirrors AdversaryLedger: JSONL, append-only,
thread-safe, state dir overridable with CALLISTO_STATE_DIR.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Iterable, Optional

from agp.adversary import (
    AdversaryLedger,
    AdversaryObjection,
)

HUMAN_CRITIC_PREFIX = "human:"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir() -> str:
    return os.environ.get(
        "CALLISTO_STATE_DIR",
        os.path.expanduser("~/.local/state/callisto"))


def human_critic_key(name: str = "owner") -> str:
    """Ledger key under which this critic's objections are filed."""
    return f"{HUMAN_CRITIC_PREFIX}{name}"


# ═════════════════════════════════════════════════════════════════════════
# The objection itself — same shape as a model's
# ═════════════════════════════════════════════════════════════════════════

VALID_SEVERITIES = ("BLOCKING", "MAJOR", "MINOR")
VALID_KINDS = ("refuting_evidence", "alternative_explanation",
               "selection_effect", "false_positive", "unspecified")


def make_human_objection(claim_id: str, text: str,
                         severity: str = "MAJOR",
                         kind: str = "unspecified",
                         axis: str = "",
                         claim_domain: str = "",
                         critic: str = "owner") -> AdversaryObjection:
    """Build an objection structurally identical to a model critic's.

    ``axis`` is free-text ('the base rate contradicts this', 'sample too
    small') and is folded into the objection text so it survives round-trips
    through the unchanged AdversaryObjection record shape. ``model`` carries
    ``human:<critic>`` so calibration_by_model files him alongside the models.
    """
    if not (text or "").strip():
        raise ValueError("an objection must say WHAT is wrong")
    sev = (severity or "").upper()
    if sev not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {VALID_SEVERITIES}, "
                         f"got {severity!r}")
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    full_text = f"{text.strip()} [axis: {axis}]" if axis.strip() else text.strip()
    return AdversaryObjection(
        claim_id=claim_id,
        text=full_text,
        kind=kind,
        severity=sev,
        model=human_critic_key(critic),
        claim_domain=claim_domain,
    )


# ═════════════════════════════════════════════════════════════════════════
# Asymmetric verdict application — same rule as the Adversary
# ═════════════════════════════════════════════════════════════════════════

def apply_human_verdict(confidence_score: float,
                        objections: Iterable[AdversaryObjection],
                        ) -> tuple[float, str]:
    """(clamped_score, veto_reason). Only ever pulls DOWN.

    Delegates to the Adversary's own asymmetric arithmetic (BLOCKING vetoes;
    otherwise score − Σ penalties, floored at 0) rather than reimplementing
    it, so human and model objections clamp identically. There is NO bonus
    path: agreement from a human raises nothing.
    """
    from agp.adversary import Adversary
    return Adversary.apply_verdict(confidence_score, list(objections))


def clamp_with_human_agreement(score: float, provenance_ceiling: float,
                               human_agrees: bool) -> float:
    """Explicit statement of the non-override rule: human agreement does not
    lift a score past what its evidence class permits. Returns
    min(score, provenance_ceiling) either way — the flag exists only to make
    the refusal legible at call sites."""
    _ = human_agrees  # deliberately unused: agreement grants nothing
    return min(max(0.0, float(score)), max(0.0, float(provenance_ceiling)))


# ═════════════════════════════════════════════════════════════════════════
# Overriding the SYSTEM — a decision with a reason, never a silent edit
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class HumanOverride:
    """One recorded instance of the human overriding the system."""
    claim_id: str
    action: str                    # forced_seal | dismissed_objection |
                                   # reopened_refused_seal | manual_resolution
    reason: str                    # REQUIRED — empty reason refuses the write
    confidence_at_decision: float = 0.0
    critic: str = "owner"
    created_at: str = field(default_factory=_now_iso)
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class OverrideLog:
    """Append-only JSONL of human overrides. Nothing deletes or edits."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(_state_dir(), "human_overrides.jsonl")
        self._lock = threading.Lock()

    def record(self, ov: HumanOverride) -> None:
        if not (ov.reason or "").strip():
            raise ValueError(
                "overruling the system requires a stated reason — "
                "a silent edit is precisely what this log exists to prevent")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ov.to_dict(), ensure_ascii=False) + "\n")

    def all(self) -> list[HumanOverride]:
        out = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # torn last line after crash — skip
                    out.append(HumanOverride(**{
                        k: v for k, v in rec.items()
                        if k in HumanOverride.__dataclass_fields__}))
        except FileNotFoundError:
            pass
        return out

    def for_claim(self, claim_id: str) -> list[HumanOverride]:
        return [o for o in self.all() if o.claim_id == claim_id]


# ═════════════════════════════════════════════════════════════════════════
# The critic surface
# ═════════════════════════════════════════════════════════════════════════

class HumanCritic:
    """Record, resolve, and score the owner's objections.

    Uses ONE shared AdversaryLedger so human and model objections live in the
    same dissent log and are scored by the same machinery; the human is
    distinguished only by the ``human:<name>`` key, which makes
    ``calibration_by_model()`` report him identically to any model.
    """

    def __init__(self, critic: str = "owner",
                 ledger: Optional[AdversaryLedger] = None,
                 overrides: Optional[OverrideLog] = None):
        self.critic = critic
        self.key = human_critic_key(critic)
        self.ledger = ledger or AdversaryLedger(os.path.join(
            _state_dir(), f"adversary_dissent.jsonl"))
        self.overrides = overrides or OverrideLog()

    # ── raising ──
    def object_to(self, claim_id: str, text: str, severity: str = "MAJOR",
                  kind: str = "unspecified", axis: str = "",
                  claim_domain: str = "") -> AdversaryObjection:
        ob = make_human_objection(
            claim_id=claim_id, text=text, severity=severity, kind=kind,
            axis=axis, claim_domain=claim_domain, critic=self.critic)
        self.ledger.record_objection(ob)
        return ob

    # ── outcomes ──
    def sustain(self, claim_id: str, objection_text: str) -> None:
        """Objection held — the seal was refused because of it."""
        self.ledger.record_sustained(claim_id, objection_text)

    def concede(self, claim_id: str, objection_text: str, reason: str) -> int:
        """Pipeline/human let the seal pass anyway. The concession itself is
        logged as an override decision with its reason (dissent kept)."""
        n = self.ledger.record_overrule(claim_id, objection_text, reason)
        if n:
            self.overrides.record(HumanOverride(
                claim_id=claim_id, action="dismissed_objection",
                reason=reason, critic=self.critic,
                detail=f"conceded objection: {objection_text[:120]}"))
        return n

    def override_system(self, claim_id: str, action: str, reason: str,
                        confidence_at_decision: float = 0.0,
                        detail: str = "") -> HumanOverride:
        """Human overrides the SYSTEM (forced_seal, reopened_refused_seal,
        manual_resolution...). Recorded as a decision with a reason — never
        a silent edit."""
        ov = HumanOverride(claim_id=claim_id, action=action, reason=reason,
                           confidence_at_decision=confidence_at_decision,
                           critic=self.critic, detail=detail)
        self.overrides.record(ov)
        return ov

    def record_resolution(self, claim_id: str, claim_was_correct: bool,
                          scoreable: bool = True) -> None:
        """Claim resolved — score every objection on it, human's included."""
        self.ledger.record_resolution(claim_id, claim_was_correct,
                                      scoreable=scoreable)

    # ── calibration ──
    def _latest(self) -> list[AdversaryObjection]:
        """This critic's objections, replayed last-wins from the shared
        ledger. AdversaryLedger._latest now provides exactly this read for
        every model key; delegating keeps one implementation of the
        append-only format instead of two that could drift."""
        return [o for o in self.ledger._latest()
                if o.model == self.key]

    def my_resolved(self, domain: Optional[str] = None):
        out = []
        for o in self._latest():
            if not o.outcome:
                continue
            if domain is not None and o.claim_domain != domain:
                continue
            out.append(o)
        return out

    def my_calibration(self) -> dict:
        """The owner's own track record, overall."""
        resolved = self.my_resolved()
        scored = [o for o in resolved if o.outcome in ("RIGHT", "WRONG")]
        right = sum(1 for o in scored if o.outcome == "RIGHT")
        raised = sum(1 for o in self._latest())
        sustained = sum(1 for o in self._latest() if o.status == "SUSTAINED")
        return {
            "critic": self.key,
            "n_raised": raised,
            "n_sustained": sustained,
            "n_scored": len(scored),
            "n_right": right,
            "precision_of_attack": round(right / len(scored), 3) if scored else None,
            "verdict": (
                "insufficient_data" if not scored
                else "well_calibrated" if right / len(scored) >= 0.35
                else "too_harsh"
            ),
        }

    def calibration_by_domain(self) -> dict[str, dict]:
        """Per-domain version of calibration_by_model: 'too harsh on FINANCIAL
        claims, well-judged on TECHNICAL ones' becomes measurable."""
        domains = sorted({o.claim_domain or "(unattributed)"
                          for o in self.my_resolved()})
        out: dict[str, dict] = {}
        for d in domains:
            resolved = self.my_resolved(None if d == "(unattributed)" else d)
            scored = [o for o in resolved if o.outcome in ("RIGHT", "WRONG")]
            right = sum(1 for o in scored if o.outcome == "RIGHT")
            out[d] = {
                "n_scored": len(scored),
                "n_right": right,
                "precision_of_attack": round(right / len(scored), 3) if scored else None,
                "verdict": (
                    "insufficient_data" if not scored
                    else "well_calibrated" if right / len(scored) >= 0.35
                    else "too_harsh"
                ),
            }
        return out


# ═════════════════════════════════════════════════════════════════════════
# CLI — no web UI by design
# ═════════════════════════════════════════════════════════════════════════

def _load_claim(store_dir: Optional[str], claim_id: Optional[str]) -> dict:
    """Best-effort lookup of a stored claim to auto-fill domain. Absence is
    not fatal: an objection may be raised against any claim id."""
    if not store_dir or not claim_id:
        return {}
    try:
        from agp.claims import ClaimStore
        c = ClaimStore(store_dir).load(claim_id)
        if c is not None:
            return {"domain": c.domain.value, "text": c.text}
    except Exception:  # noqa: BLE001 — CLI convenience must never crash the record
        pass
    return {}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m agp.human_critic",
        description="Record and score the owner's objections as a critic.")
    p.add_argument("--state-dir", default=None,
                   help="override CALLISTO_STATE_DIR")
    p.add_argument("--critic", default="owner")
    sub = p.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("object", help="raise an objection against a claim")
    po.add_argument("claim_id")
    po.add_argument("text")
    po.add_argument("--severity", default="MAJOR",
                    choices=[s.lower() for s in VALID_SEVERITIES])
    po.add_argument("--kind", default="unspecified", choices=VALID_KINDS)
    po.add_argument("--axis", default="")
    po.add_argument("--domain", default="")
    po.add_argument("--store", default=None,
                    help="ClaimStore dir to auto-lookup the claim's domain")

    pv = sub.add_parser("veto", help="objection held: seal refused")
    pv.add_argument("claim_id")
    pv.add_argument("text")

    pc = sub.add_parser("concede", help="seal passed over the objection")
    pc.add_argument("claim_id")
    pc.add_argument("text")
    pc.add_argument("--reason", required=True)

    pr = sub.add_parser("resolve", help="score objections against the outcome")
    pr.add_argument("claim_id")
    pr.add_argument("outcome", choices=["correct", "incorrect", "unscoreable"])

    pd = sub.add_parser("override", help="record a decision overriding the system")
    pd.add_argument("claim_id")
    pd.add_argument("action", choices=["forced_seal", "dismissed_objection",
                                       "reopened_refused_seal",
                                       "manual_resolution"])
    pd.add_argument("--reason", required=True)
    pd.add_argument("--confidence", type=float, default=0.0)

    pk = sub.add_parser("calibrate", help="show the owner's track record")

    a = p.parse_args(argv)
    if a.state_dir:
        os.environ["CALLISTO_STATE_DIR"] = a.state_dir

    hc = HumanCritic(critic=a.critic)

    if a.cmd == "object":
        extra = _load_claim(a.store, a.claim_id)
        ob = hc.object_to(
            a.claim_id, a.text, severity=a.severity.upper(), kind=a.kind,
            axis=a.axis, claim_domain=a.domain or extra.get("domain", ""))
        print(json.dumps(ob.to_dict(), indent=2))
    elif a.cmd == "veto":
        hc.sustain(a.claim_id, a.text)
        print(f"sustained: {a.claim_id}")
    elif a.cmd == "concede":
        n = hc.concede(a.claim_id, a.text, a.reason)
        print(f"overruled {n} objection(s); reason logged")
    elif a.cmd == "resolve":
        was_correct = {"correct": True, "incorrect": False,
                       "unscoreable": False}[a.outcome]
        hc.record_resolution(a.claim_id, was_correct,
                             scoreable=(a.outcome != "unscoreable"))
        print(f"resolution recorded for {a.claim_id}")
    elif a.cmd == "override":
        ov = hc.override_system(a.claim_id, a.action, a.reason,
                                confidence_at_decision=a.confidence)
        print(json.dumps(ov.to_dict(), indent=2))
    elif a.cmd == "calibrate":
        print(json.dumps({
            "overall": hc.my_calibration(),
            "by_domain": hc.calibration_by_domain(),
            "overrides": [o.to_dict() for o in hc.overrides.all()],
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
