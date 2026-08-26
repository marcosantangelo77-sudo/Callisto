"""The WhyExplanation record and its narrative rendering."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from tools.whyexp.records import (
    SCHEMA_VERSION,
    CeilingWhy,
    EvidenceWhy,
    IndependenceWhy,
    ObjectionWhy,
    RejectedWhy,
    StepWhy,
)


@dataclass
class WhyExplanation:
    root_query: str
    sealed: bool
    refusal_reason: str = ""
    score: float = 0.0
    tier: str = ""
    proposed: float = 0.0           # best leaf confidence entering the parent clamp
    evidence: list[EvidenceWhy] = field(default_factory=list)
    ceilings: list[CeilingWhy] = field(default_factory=list)
    objections: list[ObjectionWhy] = field(default_factory=list)
    total_penalty: float = 0.0
    independence: Optional[IndependenceWhy] = None
    rejected: list[RejectedWhy] = field(default_factory=list)
    steps: list[StepWhy] = field(default_factory=list)
    largest_constraint: str = ""
    # Stale descendants are unresolved-at-deadline records — never resolved
    # evidence. Reported separately so the explanation cannot imply they
    # counted toward the inheritance lift.
    stale_descendants: int = 0
    stale_penalty_applied: float = 0.0

    # ── machine-readable ────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "root_query": self.root_query,
            "sealed": self.sealed,
            "refusal_reason": self.refusal_reason,
            "confidence_score": self.score,
            "tier": self.tier,
            "proposed_confidence": self.proposed,
            "evidence": [e.to_dict() for e in self.evidence],
            "ceilings": [c.to_dict() for c in self.ceilings],
            "objections": [o.to_dict() for o in self.objections],
            "adversary_total_penalty": self.total_penalty,
            "independence": self.independence.to_dict()
            if self.independence else None,
            "rejected_at_ingestion": [r.to_dict() for r in self.rejected],
            "score_walk": [s.to_dict() for s in self.steps],
            "largest_constraint": self.largest_constraint,
            "stale_descendants": self.stale_descendants,
            "stale_penalty_applied": self.stale_penalty_applied,
        }

    # ── plain language ──────────────────────────────────────────────────

    def narrative(self) -> str:
        lines: list[str] = []
        head = f"WHY {self.score:.2f}" if not self.refusal_reason \
            else "WHY REFUSED"
        tier_bit = f" ({self.tier})" if self.tier else ""
        lines.append(f'{head}{tier_bit} — "{self.root_query}"')
        lines.append("")

        lines.append(f"EVIDENCE ({len(self.evidence)} item(s))")
        if self.evidence:
            for e in self.evidence:
                lines.append(f"  - [{e.source_class:<9}] {e.label}: "
                             f"{e.reason} Ceiling {e.ceiling:.2f}.")
        else:
            lines.append("  - none admitted.")
        lines.append("")

        lines.append("CONSTRAINTS ON THE SCORE")
        if self.ceilings:
            for c in self.ceilings:
                star = "  <-- BINDING" if c.binding else ""
                val = f"{c.value:.2f}" if c.value is not None else "—"
                lines.append(f"  - {c.kind}: ceiling {val}. {c.detail}{star}")
        else:
            lines.append("  - no structural ceiling applied.")
        if self.stale_descendants:
            lines.append(
                f"  - staleness: {self.stale_descendants} stale descendant "
                "record(s) are NOT resolved evidence; they apply a "
                f"-{self.stale_penalty_applied:.2f} staleness penalty only.")
        lines.append("")

        lines.append(f"ADVERSARY ({len(self.objections)} objection(s), "
                     f"-{self.total_penalty:.2f} total)")
        if self.objections:
            for o in self.objections:
                cost = "VETO" if o.veto else f"-{o.penalty:.2f}"
                status = f" [{o.status}]" if o.status else ""
                lines.append(f"  - [{o.severity}/{o.kind}] {cost}{status}: "
                             f"{o.text}")
        else:
            lines.append("  - the conclusion withstood attack unchanged.")
        lines.append("")

        if self.independence:
            ind = self.independence
            lines.append(f"INDEPENDENCE: {ind.n_fetches} fetch(es) -> "
                         f"{ind.n_independent} independent source(s)")
            for c in ind.collapses:
                lines.append(f"  - {c}")
            lines.append("")

        if self.rejected:
            lines.append(f"REJECTED AT INGESTION ({len(self.rejected)})")
            for r in self.rejected:
                cov = (f" (relevance {r.relevance_score:.0%})"
                       if r.relevance_score >= 0 else "")
                src = f"[{r.source_name}] " if r.source_name else ""
                lines.append(f"  - {src}{r.reason}{cov}")
            lines.append("")

        if self.proposed > 0:
            lines.append("SCORE WALK")
            lines.append(f"  start {self.proposed:.2f} "
                         "(best leaf confidence)")
            if not self.steps:
                lines.append("  =   unchanged  (proposal already sat at or "
                             "below every binding ceiling)")
            for s in self.steps:
                delta = abs(s.before - s.after)
                if delta > 1e-9:
                    sign = "-" if s.after < s.before else "+"
                    lines.append(
                        f"  {sign}{delta:.2f}  {s.stage} ({s.rule})"
                        f" -> {s.after:.2f}")
                else:
                    lines.append(f"  =   unchanged  {s.stage} ({s.rule})")
            lines.append("")

        lines.append(f"THE SHORT ANSWER: {self.largest_constraint}")
        if self.refusal_reason:
            lines.append(f"REFUSAL: {self.refusal_reason}")
        return "\n".join(lines)
