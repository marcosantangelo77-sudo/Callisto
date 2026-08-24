"""
THE ADVERSARY — fourth AGP role (NEXT.md §3 cheap win 1, §5 dissent logging,
§multi-model role assignment).

Nothing else in the system is incentivised to be wrong. The Sentinel VETOES
but never ATTACKS; before this module its only call site was a 32-token
domain classifier. The Adversary's only job is to try to FALSIFY a
conclusion before it seals:

  - what evidence would refute it?
  - what alternative explanation fits the same facts?
  - what selection effect could produce this pattern without the mechanism?
  - what is the most likely way this is a false positive?

Three structural properties, all enforced here:

1. ASYMMETRY. The adversary can only LOWER a confidence score or VETO the
   seal outright — the same one-directional rule as clamp_parent_confidence.
   A critic that can bless a conclusion is not a critic. Every clamp in this
   module is min(raw, something) or raw − penalty, floored at 0.

2. SCORED TRACK RECORD. Every objection is recorded with whether the seal
   went through (SUSTAINED = seal blocked, OVERRULED = human/pipeline let it
   pass over the objection) and — once the claim resolves — whether the
   objection was RIGHT (claim resolved against the conclusion) or WRONG.
   An unscored adversary becomes a rubber stamp in the other direction;
   calibration stats on this record are what let the critic itself be tuned.

3. DISSENT LOGGING. When an objection is overruled, the objection, the
   reasoning that overruled it, and the eventual outcome stay in the log as
   append-only JSONL. Disagreement becomes training data.

Domain-general throughout: nothing here knows what a bet, a coin, or a
protein is. The attack prompt speaks of claims, evidence, mechanisms.
"""

from agp.thresholds import floor_conf
import json
import os
import threading
from dataclasses import dataclass, field, asdict
import math
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

# ── Confidence vocabulary ─────────────────────────────────────────────────
# Mirrors agp.thresholds tier bands without importing them (keeps this module
# dependency-light for tests); values must stay consistent.

TIER_SPECULATIVE_MAX = 0.54   # top of SPECULATIVE band


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


# ═════════════════════════════════════════════════════════════════════════
# Role-level model assignment (NEXT.md multi-model section)
# ═════════════════════════════════════════════════════════════════════════

class AGPRole:
    """AGP roles mapped to ProviderRouter task classes.

    The router (inference.ProviderRouter) routes task_class → tier →
    endpoint pool from config/providers.yaml, so assigning a model per ROLE
    means declaring which task_class the role's judgments go through —
    never hardcoding a provider or model name (BUILD_MANDATE §1).
    """

    ARCHITECT = "Architect"
    MANAGER = "Manager"
    SENTINEL = "Sentinel"
    ADVERSARY = "Adversary"

    # Frontier where judgment is scarce (framing, criticism); local where
    # volume dominates (search/collate/extract). These are DEFAULTS —
    # config/providers.yaml remains the source of truth for endpoints.
    ROLE_TASK_CLASSES = {
        ARCHITECT: ["hypothesis_generation", "research_synthesis"],
        MANAGER: ["extraction", "classification", "screening"],
        SENTINEL: ["adversarial_review"],
        ADVERSARY: ["adversarial_review"],
    }


# ═════════════════════════════════════════════════════════════════════════
# Ensemble disagreement as a confidence input
# ═════════════════════════════════════════════════════════════════════════

# Where independently-routed evaluations of the SAME claim diverge more than
# this much (probability spread), genuine uncertainty has been located and
# the confidence ceiling drops hard. Agreement is weak evidence; disagreement
# is strong evidence. Most systems average this away — we refuse to.
DISAGREEMENT_SPREAD_THRESHOLD = 0.30
DISAGREEMENT_CEILING = TIER_SPECULATIVE_MAX      # capped below CORROBORATED
MILD_DISAGREEMENT_CEILING = 0.70                 # spread ≥ half threshold


def ensemble_ceiling(evaluations: Iterable[float]) -> Optional[float]:
    """Confidence CEILING implied by ensemble spread, or None if no cap applies.

    evaluations: independently-produced probabilities for the same claim
    (different models/configurations via different router endpoints).

    Asymmetric by construction: the returned value can only ever be USED as
    min(score, ceiling). Wide spread caps below PROBABLE; moderate spread
    caps at 0.70; tight agreement caps at None (no restriction — agreement
    never RAISES anything here either).
    """
    xs = [max(0.0, min(1.0, float(x))) for x in evaluations]
    if len(xs) < 2:
        return None
    spread = max(xs) - min(xs)
    if spread >= DISAGREEMENT_SPREAD_THRESHOLD:
        return DISAGREEMENT_CEILING
    if spread >= DISAGREEMENT_SPREAD_THRESHOLD / 2:
        return MILD_DISAGREEMENT_CEILING
    return None


def clamp_with_ensemble(score: float,
                        evaluations: Iterable[float]) -> tuple[float, str]:
    """(clamped_score, reason). Only ever pulls DOWN."""
    s = max(0.0, min(1.0, float(score)))
    ceil_ = ensemble_ceiling(evaluations)
    if ceil_ is None or s <= ceil_:
        # FLOOR, not round: round(0.836, 2) == 0.84 raises the score by 0.004.
        # Trivial in magnitude, but this function's contract is "only ever pulls
        # DOWN", and in a system whose premise is that no automated actor can
        # inflate confidence, an invariant broken trivially is still broken —
        # and repeated round-trips compound it.
        return math.floor(s * 100) / 100, ""
    return (
        math.floor(ceil_ * 100) / 100,
        f"ensemble disagreement: spread across {len(set(round(x, 4) for x in evaluations))} "
        f"evaluations exceeds threshold; ceiling lowered to {ceil_}",
    )


# ═════════════════════════════════════════════════════════════════════════
# Objections, dissent log, scored track record
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class AdversaryObjection:
    """One falsification attack produced by the adversary."""
    claim_id: str                       # stable id of the claim/session attacked
    text: str                           # the objection itself
    kind: str = "unspecified"           # refuting-evidence | alternative-explanation |
                                        # selection-effect | false-positive | unspecified
    severity: str = "MINOR"             # BLOCKING | MAJOR | MINOR
    created_at: str = field(default_factory=_now_iso)
    model: str = ""                     # which endpoint produced it (router-reported)
    claim_domain: str = ""              # claim class (Domain name) — per-model×domain calibration
    # Lifecycle (mutated only through TrackRecord/DissentLog methods):
    status: str = "RAISED"              # RAISED → SUSTAINED | OVERRULED
    overrule_reasoning: str = ""        # why the pipeline sealed anyway
    resolution: str = ""                # PENDING until claim resolves
    outcome: str = ""                   # RIGHT | WRONG | UNSCOREABLE after resolution

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_blocking(self) -> bool:
        return self.severity == "BLOCKING"

    @property
    def penalty(self) -> float:
        """Confidence penalty this objection applies. BLOCKING objections do
        not penalize — they veto. Severity → penalty, monotone, additive."""
        return {"BLOCKING": 0.0, "MAJOR": 0.15, "MINOR": 0.05}.get(
            self.severity.upper(), 0.05)


class AdversaryLedger:
    """Append-only dissent log + scored track record for the Adversary.

    Backed by a JSONL file (one objection per line) so it survives process
    restarts and stays auditable by eye. Thread-safe. Nothing may delete or
    rewrite an entry — overrulings and resolutions APPEND fields by rewriting
    the in-memory copy and appending a marker line; the original raise is
    never edited.

    Calibration: accuracy() reports, of RESOLVED objections, how often the
    adversary was RIGHT. This number is what distinguishes a real critic
    from a rubber stamp — too-harsh and too-soft critics both show up here.
    """

    def __init__(self, path: Optional[str] = None):
        if path is None:
            state_dir = os.environ.get(
                "CALLISTO_STATE_DIR",
                os.path.expanduser("~/.local/state/callisto"))
            path = os.path.join(state_dir, "adversary_dissent.jsonl")
        self.path = path
        self._lock = threading.Lock()
        self._objections: dict[str, list[AdversaryObjection]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # torn last line after crash — skip
                    ob = AdversaryObjection(**{k: v for k, v in rec.items()
                                               if k in AdversaryObjection.__dataclass_fields__})
                    self._objections.setdefault(ob.claim_id, []).append(ob)
        except FileNotFoundError:
            pass

    def _append(self, ob: AdversaryObjection) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ob.to_dict(), ensure_ascii=False) + "\n")

    # ── raising ──
    def record_objection(self, objection: AdversaryObjection) -> None:
        with self._lock:
            self._objections.setdefault(objection.claim_id, []).append(objection)
            self._append(objection)

    # ── overruling (dissent logging) ──
    def record_overrule(self, claim_id: str, objection_text: str,
                        overrule_reasoning: str) -> int:
        """Mark matching RAISED objections OVERRULED, logging the reasoning
        that overruled them. Returns count updated. Appends updated copies;
        originals remain earlier in the file."""
        n = 0
        with self._lock:
            for ob in self._objections.get(claim_id, []):
                if ob.status == "RAISED" and ob.text == objection_text:
                    ob.status = "OVERRULED"
                    ob.overrule_reasoning = overrule_reasoning
                    self._append(ob)
                    n += 1
        return n

    # ── sealing decision ──
    def record_sustained(self, claim_id: str, objection_text: str) -> None:
        """Objection held: the seal was refused because of it."""
        with self._lock:
            for ob in self._objections.get(claim_id, []):
                if ob.status == "RAISED" and ob.text == objection_text:
                    ob.status = "SUSTAINED"
                    self._append(ob)

    # ── resolution scoring ──
    def record_resolution(self, claim_id: str, claim_was_correct: bool,
                          scoreable: bool = True) -> None:
        """Score every objection on this claim against what actually happened.

        An objection was RIGHT when it attacked a conclusion that turned out
        WRONG (or raised a real flaw the resolution confirmed); WRONG when
        the claim survived. Unscoreable objections (e.g. procedural) are
        marked UNSCOREABLE and excluded from accuracy.
        """
        with self._lock:
            for ob in self._objections.get(claim_id, []):
                if ob.resolution != "PENDING" or not ob.resolution:
                    ob.resolution = _now_iso()[:10]
                    if not scoreable:
                        ob.outcome = "UNSCOREABLE"
                    else:
                        ob.outcome = ("RIGHT" if not claim_was_correct else "WRONG")
                    self._append(ob)

    # ── queries ──
    def objections_for(self, claim_id: str) -> list[AdversaryObjection]:
        return list(self._objections.get(claim_id, []))

    def all_resolved(self) -> list[AdversaryObjection]:
        out = []
        seen = set()
        for obs in self._objections.values():
            for ob in obs:
                key = (ob.claim_id, ob.created_at, ob.text)
                if key in seen:
                    continue  # later appends supersede earlier lines
                seen.add(key)
                out.append(ob)
        return [o for o in out if o.outcome]

    def calibration(self) -> dict:
        """Track-record summary: is the critic too harsh, too soft, or honest?

        precision_of_attack: of objections that could be scored, fraction RIGHT.
          High   → critic catches real flaws (keep).
          Low    → critic attacks good conclusions (too harsh — soften).
        Also reports volume so 'critic says nothing' shows up as n=0, not as
        a flattering ratio.
        """
        resolved = self.all_resolved()
        scored = [o for o in resolved if o.outcome in ("RIGHT", "WRONG")]
        right = sum(1 for o in scored if o.outcome == "RIGHT")
        total_raised = len({id(o) for obs in self._objections.values() for o in obs})
        sustained = sum(1 for obs in self._objections.values()
                        for o in obs if o.status == "SUSTAINED")
        return {
            "n_raised": total_raised,
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

    def calibration_by_model(self, domain: Optional[str] = None) -> dict:
        """Per-model critic scores — the record empirical routing consumes
        (NEXT.md dissent logging; W4 multi-model). Keyed by the ``model`` that
        produced each objection, optionally narrowed to one claim class, so
        'harsh on financial claims, soft on scientific ones' becomes measurable.

        Returns {model: score_record}; models with no scored objections report
        n_scored=0 rather than an implied 100% or 0%. The empty-string model
        (backend failure / unattributed) is grouped under "(unattributed)".
        """
        out: dict[str, list] = {}
        for o in self.all_resolved():
            if domain is not None and o.claim_domain != domain:
                continue
            key = o.model or "(unattributed)"
            out.setdefault(key, []).append(o)
        scores: dict[str, dict] = {}
        for model, obs in sorted(out.items()):
            scored = [o for o in obs if o.outcome in ("RIGHT", "WRONG")]
            right = sum(1 for o in scored if o.outcome == "RIGHT")
            scores[model] = {
                "n_scored": len(scored),
                "n_right": right,
                "precision_of_attack": round(right / len(scored), 3) if scored else None,
                "verdict": (
                    "insufficient_data" if not scored
                    else "well_calibrated" if right / len(scored) >= 0.35
                    else "too_harsh"
                ),
            }
        return scores


# ═════════════════════════════════════════════════════════════════════════
# The Adversary itself
# ═════════════════════════════════════════════════════════════════════════

ATTACK_SYSTEM_PROMPT = """You are THE ADVERSARY in a research-integrity \
pipeline. Your ONLY job is to build the strongest honest case that the \
conclusion below is WRONG. You are not a reviewer and you must not be polite. \
Do not comment on strengths. Do not weigh both sides. Attack.

Attack along exactly four axes:
1. REFUTING EVIDENCE — what specific evidence, if it existed or was missed, \
would refute this conclusion? Is any already present in the evidence and \
underweighted?
2. ALTERNATIVE EXPLANATION — what different mechanism explains the same facts \
at least as well?
3. SELECTION EFFECT — what process could produce this observed pattern WITHOUT \
the claimed mechanism (survivorship, publication bias, multiple comparisons, \
data snooping)?
4. FALSE POSITIVE — what is the single most likely way this conclusion is \
simply an artifact?

Rules: every objection must name the concrete fact or gap it rests on — no \
generic skepticism. If the conclusion genuinely withstands you, return zero \
objections rather than manufacturing weak ones; a critic who always objects \
is ignored, and being calibrated matters more than being fierce."""

VERDICT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"enum": ["refuting_evidence",
                                      "alternative_explanation",
                                      "selection_effect",
                                      "false_positive"]},
                    "severity": {"enum": ["BLOCKING", "MAJOR", "MINOR"]},
                    "text": {"type": "string"},
                },
                "required": ["kind", "severity", "text"],
            },
        }
    },
    "required": ["objections"],
}


def _attack_user_prompt(conclusion: str, evidence_items: Iterable[str],
                        reasoning: str = "") -> str:
    ev = "\n".join(f"- {e}" for e in evidence_items) or "(none)"
    r = f"\nReasoning chain:\n{reasoning}\n" if reasoning else "\n"
    return (f"CONCLUSION UNDER ATTACK:\n{conclusion}\n{r}"
            f"EVIDENCE IT RESTS ON:\n{ev}\n\n"
            "Return JSON: {\"objections\": [{kind, severity, text}, ...]} "
            "— empty list if the conclusion withstands your best attack.")


class Adversary:
    """Fourth AGP role. Attacks a conclusion before it seals.

    Construction is backend-agnostic. Pass any object exposing
    ``await complete(task_class, messages, schema=...)`` — in production the
    ProviderRouter (inference.py); in tests, a stub. The task_class comes
    from ROLE_TASK_CLASSES[ADVERSARY] so model-per-role stays a config
    concern, never a hardcoded provider.

    ASYMMETRY GUARANTEE: ``apply_verdict`` and ``make_seal_veto`` can only
    lower confidence or refuse the seal. There is no code path in this
    module that increases a score.
    """

    def __init__(self, router, ledger: Optional[AdversaryLedger] = None,
                 task_class: Optional[str] = None):
        self.router = router
        self.ledger = ledger or AdversaryLedger()
        self.task_class = task_class or AGPRole.ROLE_TASK_CLASSES[AGPRole.ADVERSARY][0]

    # ── the attack ──
    async def attack(self, claim_id: str, conclusion: str,
                     evidence_items: Iterable[str], reasoning: str = "") -> list[AdversaryObjection]:
        """Run one falsification attempt. Returns objections (possibly empty),
        all recorded in the ledger. Router failure FAILS CLOSED: a crash is
        surfaced as a BLOCKING objection rather than silently passing the
        conclusion — the seal path treats reviewer failure as refusal."""
        messages = [
            {"role": "system", "content": ATTACK_SYSTEM_PROMPT},
            {"role": "user",
             "content": _attack_user_prompt(conclusion, evidence_items, reasoning)},
        ]
        model_name = ""
        try:
            resp = await self.router.complete(
                self.task_class, messages, schema=VERDICT_JSON_SCHEMA)
            parsed = resp.get("parsed_json")
            model_name = resp.get("model", "")
            self.last_model = model_name
            if parsed is None:
                parsed = json.loads(resp.get("content") or "{}")
        except Exception as e:  # noqa: BLE001 — fail closed by design
            return [AdversaryObjection(
                claim_id=claim_id,
                text=f"adversary backend failed ({type(e).__name__}: {e}) — "
                     f"conclusion unattacked, refusing by default",
                kind="false_positive", severity="BLOCKING", model=""),
            ]
        objections: list[AdversaryObjection] = []
        for raw in (parsed or {}).get("objections", []) or []:
            if not isinstance(raw, dict) or not (raw.get("text") or "").strip():
                continue  # empty-text objections are noise, drop them
            objections.append(AdversaryObjection(
                claim_id=claim_id,
                text=str(raw["text"]).strip(),
                kind=str(raw.get("kind", "unspecified")),
                severity=str(raw.get("severity", "MINOR")).upper(),
                model=model_name,
            ))
        for ob in objections:
            self.ledger.record_objection(ob)
        return objections

    # ── applying the verdict (asymmetric) ──
    @staticmethod
    def apply_verdict(confidence_score: float,
                      objections: Iterable[AdversaryObjection]) -> tuple[float, str]:
        """Return (clamped_score, veto_reason).

        - Any BLOCKING objection → veto_reason set (seal must refuse).
        - Otherwise score − Σ penalties (MAJOR/MINOR), floored at 0.
        - No objections → score unchanged. There is NO bonus path.
        """
        objs = list(objections)
        for ob in objs:
            if ob.is_blocking:
                return floor_conf(max(0.0, float(confidence_score))), ob.text
        penalty = sum(o.penalty for o in objs)
        clamped = floor_conf(max(0.0, float(confidence_score) - penalty))
        reason = ""
        if objs and clamped < floor_conf(float(confidence_score)):
            reason = f"adversary: {len(objs)} objection(s), -{penalty:.2f} confidence"
        return clamped, reason

    # ── seal-path wiring (the existing seal_veto hook) ──
    def make_seal_veto(self, persist: bool = True) -> Callable:
        """Build the callable to assign to ``session.seal_veto``.

        Signature matches AGPSession.seal(): (session, summary) → Optional[str].
        Runs the attack SYNCHRONOUSLY-wrapped around the async backend via a
        fresh event loop (seal() is sync); objections are recorded, then:

          BLOCKING objection  → truthy reason → seal refused (fails closed,
                                including on backend failure inside attack()).
          Non-blocking        → summary.confidence_score lowered in place
                                (only down), reason logged, seal proceeds.
          No objections       → None; nothing raised, ever.

        On success the sustained/overruled statuses are persisted so the
        track record reflects what actually happened at seal time.
        """
        adversary = self

        def _veto(session, summary) -> Optional[str]:
            import asyncio
            claim_id = getattr(session, "session_id", None) or claim_default(session)
            evidence_texts = [e.content for e in getattr(session, "evidence", [])]
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                objections = loop.run_until_complete(adversary.attack(
                    claim_id, summary.conclusion, evidence_texts,
                    reasoning=getattr(summary, "scope", "")))
            else:
                objections = asyncio.run(adversary.attack(
                    claim_id, summary.conclusion, evidence_texts,
                    reasoning=getattr(summary, "scope", "")))

            new_score, reason = adversary.apply_verdict(
                summary.confidence_score, objections)
            blocking = [o for o in objections if o.is_blocking]
            if blocking:
                for o in blocking:
                    adversary.ledger.record_sustained(claim_id, o.text)
                return f"adversary veto: {blocking[0].text}"
            if reason and new_score < summary.confidence_score:
                summary.confidence_score = new_score
                session.add_manager_objection(reason)
            # Non-blocking objections left RAISED: the pipeline chose to seal
            # anyway — log them as overruled-with-reason so dissent is kept.
            for o in objections:
                adversary.ledger.record_overrule(
                    claim_id, o.text,
                    "sealed over objection: non-blocking severity; "
                    f"confidence reduced to {summary.confidence_score}")
            return None

        def claim_default(_session) -> str:
            return "unknown-claim"

        return _veto


def install_adversary(session, adversary: "Adversary") -> Callable:
    """Wire an Adversary into an AGPSession's seal path.

    Returns the veto callable actually installed. Idempotent per session:
    installing twice replaces the previous adversary hook (last writer wins,
    and callers can compare the returned callables to detect that).
    """
    veto = adversary.make_seal_veto()
    session.seal_veto = veto
    return veto