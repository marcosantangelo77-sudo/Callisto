"""
CROSS-MODEL ENSEMBLE — W4. A model reviewing its own conclusion shares its own
blind spots; a different model, from a different training distribution, is
structurally harder to fool. And where independent models disagree about the
SUBSTANCE of a conclusion, that disagreement is evidence of genuine uncertainty
— data to hand a human, never noise to average away.

Four properties, all enforced here:

1. MODEL DISTINCTNESS IS VISIBLE. When an adversary resolves to the same model
   that produced the conclusion, the record says SELF-REVIEW and the confidence
   ceiling drops (SELF_REVIEW_CEILING). A self-review is weaker evidence than
   an independent one; nothing may silently treat them as equivalent.

2. MULTI-ADVERSARY. N adversaries attack one conclusion; their objections are
   pooled. Unanimous unrebutted objection weighs more than a lone one
   (UNANIMITY_BONUS_PENALTY). Agreement among independent critics that the
   conclusion is sound is stronger evidence than one approval — but agreement
   still may not RAISE confidence, per the system-wide asymmetry.

3. DISAGREEMENT AS A MEASURED SIGNAL. ensemble_ceiling (adversary.py) measures
   spread over confidence NUMBERS. This module extends it: when critics diverge
   on substance — one attacks representativeness, another does not — the
   divergence is captured as a DisagreementRecord naming WHAT they disagree
   about, for a human to read, not just a lowered score.

4. HONEST DEGRADATION. One backend available → everything still works, clearly
   marked self-review. No backends at all → fail closed, as everywhere else.

Domain-general throughout: models, claims, evidence — no domain vocabulary.
"""

from agp.thresholds import floor_conf
import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from agp.adversary import (
    Adversary,
    AdversaryObjection,
    TIER_SPECULATIVE_MAX,
    clamp_with_ensemble,
    ensemble_ceiling,
)

# ── Ceilings ────────────────────────────────────────────────────────────────

#: A conclusion reviewed only by its own author's model cannot exceed this.
#: Same band logic as DISAGREEMENT_CEILING: capped below CORROBORATED, because
#: "the model that wrote it agrees with itself" is corroboration by definition.
SELF_REVIEW_CEILING = TIER_SPECULATIVE_MAX

#: When EVERY independent adversary raises an unrebutted objection, the attack
#: is not noise — apply this extra penalty beyond the summed per-objection
#: penalties. Objections can subtract more; never less than zero.
UNANIMITY_BONUS_PENALTY = 0.10


def normalize_model(name: str) -> str:
    """Canonical model identity for the distinctness comparison.

    Providers report the same model under different spellings ('openai/gpt-4o',
    'GPT-4o', 'gpt-4o-2024-08'). Compare on lowercase base name: strip an
    optional provider prefix, a date/build suffix, and surrounding whitespace.
    Two different providers' endpoints serving genuinely different weights
    should be configured with distinguishable names — ambiguity resolves to
    SAME model, i.e. the conservative reading (self-review, lower ceiling).
    """
    n = (name or "").strip().lower()
    if "/" in n:
        n = n.rsplit("/", 1)[-1]
    # drop trailing build/date tags: gpt-4o-2024-08-06 → gpt-4o
    n = re.sub(r"-20\d{2}(-\d{2}){0,2}$", "", n)
    return n.strip()


# Suffixes that mark an endpoint as a proxy/mirror/alias of the SAME weights
# behind the base name, not a genuinely different reviewer (F6b: distinctness
# was judged on spelling, so a model could review its own conclusion through
# an alias and the SELF_REVIEW_CEILING never applied). Anything sharing a base
# name with one of these markers attached resolves to the same identity.
_ALIAS_MARKERS = ("-proxy", "-alias", "-mirror", "-replica")


def _same_weights(a: str, b: str) -> bool:
    """True iff two normalized model names denote the same underlying weights.
    Conservative: a name that is exactly the other name PLUS an alias marker
    ('gpt-4o-proxy-alias' vs 'gpt-4o') counts as the SAME model."""
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    for marker in _ALIAS_MARKERS:
        rest = longer.split(marker, 1)[0]
        if longer.startswith(shorter + marker) and rest == shorter:
            return True
    return False


@dataclass
class ReviewProvenance:
    """Whether the review was independent of the conclusion's author, VISIBLE
    in the record rather than silently assumed."""
    author_model: str
    reviewer_models: list[str] = field(default_factory=list)

    @property
    def independent(self) -> bool:
        """True iff at least one reviewer is a DIFFERENT model from the author.
        An unknown reviewer ('' after normalization) is never evidence of
        independence — ambiguity resolves conservative. Neither is an UNKNOWN
        AUTHOR: with author_model='' nothing distinguishes the reviewer from
        the model that wrote the conclusion, so the review counts as
        self-review (F6a — otherwise ANY reviewer reads as independent and
        the SELF_REVIEW_CEILING can never engage on paths that don't record
        authorship)."""
        a = normalize_model(self.author_model)
        if not a:
            return False
        return any(m not in ("", "(unattributed)")
                   and not _same_weights(normalize_model(m), a)
                   for m in self.reviewer_models)

    @property
    def mode(self) -> str:
        return "independent_review" if self.independent else "self_review"

    @property
    def ceiling(self) -> Optional[float]:
        """Confidence ceiling implied by who reviewed. Independent review → no
        cap from provenance alone; self-review → capped at SPECULATIVE."""
        return None if self.independent else SELF_REVIEW_CEILING

    def to_dict(self) -> dict:
        return {"author_model": self.author_model,
                "reviewer_models": list(self.reviewer_models),
                "mode": self.mode,
                "ceiling": self.ceiling}


# ── Substantive disagreement ────────────────────────────────────────────────

KIND_LABELS = {
    "refuting_evidence": "whether refuting evidence exists or was missed",
    "alternative_explanation": "what mechanism best explains the facts",
    "selection_effect": "whether a selection effect could produce the pattern",
    "false_positive": "whether the conclusion is an artifact",
}


@dataclass
class DisagreementRecord:
    """WHAT independent models disagree about — not just that scores spread.

    'Two models diverge on whether the sample is representative' is worth more
    to a human than a lowered number. Records are append-only evidence of
    located uncertainty; they never feed back upward into confidence."""
    topic_kind: str                 # objection kind the divergence centres on
    attacking_models: list[str]     # models raising the objection
    non_attacking_models: list[str] # independent reviewers silent on this axis
    detail: str                     # the objection text itself

    def describe(self) -> str:
        label = KIND_LABELS.get(self.topic_kind, self.topic_kind)
        attackers = ", ".join(self.attacking_models)
        others = ", ".join(self.non_attacking_models) or "no other independent reviewer"
        return (f"Models diverge on {label}: {attackers} object"
                f" ('{self.detail}'); {others} does not.")


def capture_substantive_disagreement(
        objections: Iterable[AdversaryObjection],
        reviewer_models: Iterable[str],
        author_model: str = "") -> list[DisagreementRecord]:
    """Where independent critics diverge ON AN AXIS of the attack, record it.

    An axis counts as substantive disagreement when at least one INDEPENDENT
    reviewer objects along it while another independent reviewer stays silent
    on it. A lone critic objecting is not disagreement between models — it is
    one opinion, already handled by the penalty path. Self-review objections
    never count as a dissenting second voice, and unattributed objections
    (model unknown — e.g. backend failure stubs) are excluded: an unknown
    critic cannot be shown to be a second, independent one.
    """
    objs = [o for o in objections]
    reviewers = [m for m in reviewer_models
                 if not author_model
                 or normalize_model(m) != normalize_model(author_model)]
    records = []
    for kind in sorted({o.kind for o in objs}):
        attackers_indep = sorted({o.model for o in objs
                                  if o.kind == kind and o.model
                                  and normalize_model(o.model) != normalize_model(author_model)})
        silent = [m for m in reviewers
                  if m not in attackers_indep]
        if attackers_indep and silent:
            detail = next(o.text for o in objs
                          if o.kind == kind and (o.model or "") in attackers_indep)
            records.append(DisagreementRecord(
                topic_kind=kind,
                attacking_models=attackers_indep,
                non_attacking_models=silent,
                detail=detail))
    return records


# ── The panel ───────────────────────────────────────────────────────────────

@dataclass
class PanelVerdict:
    """Pooled result of N adversaries attacking one conclusion."""
    objections: list[AdversaryObjection] = field(default_factory=list)
    provenance: Optional[ReviewProvenance] = None
    disagreements: list[DisagreementRecord] = field(default_factory=list)
    ensemble_spread_ceiling: Optional[float] = None   # numeric-spread signal
    backend_failures: int = 0

    @property
    def has_blocking(self) -> bool:
        return any(o.is_blocking for o in self.objections)

    @property
    def unanimous_unrebutted(self) -> bool:
        """Every INDEPENDENT reviewer raised an objection, none sustained-away
        yet — the pooled attack reads as consensus, not one grumpy critic."""
        prov = self.provenance
        if not prov or not prov.independent:
            return False
        indep = [normalize_model(m) for m in prov.reviewer_models
                 if normalize_model(m) != normalize_model(prov.author_model)]
        attackers = {normalize_model(o.model or "(unattributed)")
                     for o in self.objections}
        return bool(indep) and all(m in attackers for m in indep)

    def apply(self, confidence_score: float) -> tuple[float, str]:
        """(clamped_score, veto_reason). ASYMMETRY: every term here is min() or
        subtraction, floored at 0. No code path raises a score."""
        s = max(0.0, min(1.0, float(confidence_score)))
        if self.has_blocking:
            block = next(o for o in self.objections if o.is_blocking)
            return floor_conf(s), f"adversary panel veto: {block.text}"

        reasons = []
        # 1. per-objection penalties (pooled across all critics)
        penalty = sum(o.penalty for o in self.objections)
        # 2. unanimity: unanimous unrebutted objection outweighs a lone one
        if self.unanimous_unrebutted:
            penalty += UNANIMITY_BONUS_PENALTY
            reasons.append(f"unanimous objection across "
                           f"{len(set(o.model for o in self.objections))} critics")
        clamped = max(0.0, s - penalty)
        # 3. provenance ceiling — self-review cannot ride above SPECULATIVE
        ceil_ = self.provenance.ceiling if self.provenance else None
        if ceil_ is not None and clamped > ceil_:
            clamped = ceil_
            reasons.append(f"{self.provenance.mode}: ceiling {ceil_}")
        # 4. numeric ensemble spread (existing ensemble_ceiling, floor-rounded)
        if self.ensemble_spread_ceiling is not None and clamped > self.ensemble_spread_ceiling:
            clamped = self.ensemble_spread_ceiling
            reasons.append("ensemble score disagreement")
        clamped = math.floor(clamped * 100 + 1e-9) / 100
        if clamped < floor_conf(s):
            default_reason = (f"adversary panel: {len(self.objections)} objection(s)"
                              f", -{penalty:.2f}")
            return clamped, "; ".join(reasons) or default_reason
        return clamped, ""


class AdversaryPanel:
    """N adversaries against one conclusion; one is the minimum.

    Each entry is a fully-constructed Adversary with its own router — different
    task_class routing means different models per NEXT.md role assignment; the
    router-reported model names are what distinctness is judged on. With ONE
    adversary everything still works and the verdict honestly reports
    self_review where the critic resolves to the author's model.
    """

    def __init__(self, adversaries: Iterable[Adversary]):
        advs = list(adversaries)
        if not advs:
            raise ValueError("AdversaryPanel needs at least one adversary; "
                             "a panel of zero must fail loudly, not pass silently")
        self.adversaries = advs

    async def attack(self, claim_id: str, conclusion: str,
                     evidence_items: Iterable[str], reasoning: str = "",
                     author_model: str = "",
                     score_evaluations: Optional[Iterable[float]] = None,
                     claim_domain: str = "") -> PanelVerdict:
        """Run every adversary concurrently, pool objections, measure
        independence and substantive disagreement. Backend failures inside an
        adversary already fail closed (its BLOCKING failure objection is part
        of the pool) — but are counted so the record shows partial coverage."""
        results = await asyncio.gather(
            *(a.attack(claim_id, conclusion, evidence_items, reasoning)
              for a in self.adversaries),
            return_exceptions=True)

        pooled: list[AdversaryObjection] = []
        failures = 0
        for r in results:
            if isinstance(r, BaseException):
                failures += 1
                pooled.append(AdversaryObjection(
                    claim_id=claim_id,
                    text=f"panel member failed ({type(r).__name__}: {r}) — "
                         f"conclusion unattacked by one critic, refusing by default",
                    kind="false_positive", severity="BLOCKING", model=""))
                continue
            # Adversary.attack fails closed internally: a dead backend surfaces
            # as its own BLOCKING objection rather than an exception. Count it.
            for ob in r:
                if ob.text.startswith("adversary backend failed"):
                    failures += 1
                if claim_domain:
                    ob.claim_domain = claim_domain
                pooled.append(ob)

        reviewer_models = []
        for a in self.adversaries:
            name = getattr(a, "last_model", "") \
                or getattr(getattr(a, "router", None), "last_model", "") \
                or getattr(a, "declared_model", "")
            reviewer_models.append(name or "(unattributed)")

        prov = ReviewProvenance(author_model=author_model,
                                reviewer_models=reviewer_models)
        disagreements = capture_substantive_disagreement(
            pooled, reviewer_models, author_model)
        spread_ceiling = None
        if score_evaluations is not None:
            spread_ceiling = ensemble_ceiling(list(score_evaluations))
        return PanelVerdict(objections=pooled, provenance=prov,
                            disagreements=disagreements,
                            ensemble_spread_ceiling=spread_ceiling,
                            backend_failures=failures)


def apply_panel_verdict(score: float, verdict: PanelVerdict,
                        evaluations: Optional[Iterable[float]] = None
                        ) -> tuple[float, str, list[DisagreementRecord]]:
    """Convenience clamp used by the seal path: panel verdict + optional numeric
    ensemble spread, one asymmetric application. Returns
    (clamped_score, reason, substantive_disagreements_for_the_human_record)."""
    if evaluations is not None and verdict.ensemble_spread_ceiling is None:
        verdict.ensemble_spread_ceiling = ensemble_ceiling(evaluations)
    clamped, reason = verdict.apply(score)
    # belt-and-braces: the standalone ensemble clamp is also downward-only;
    # take the min of the two readings so neither path can leak a raise.
    if evaluations is not None:
        alt, alt_reason = clamp_with_ensemble(clamped, evaluations)
        if alt < clamped:
            clamped, reason = alt, (reason + "; " + alt_reason).strip("; ")
    return clamped, reason, list(verdict.disagreements)