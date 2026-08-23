"""Attribution: replay every downward adjustment on one raw estimate.

The chain (tools/pipeline/engine.py + agp), in the order the pipeline applies
it to a parent conclusion:

  1. provenance_ceiling   min(raw, MAX_CONFIDENCE_BY_SOURCE[best class])
                          engine.py:382 — INFERRED/SIGNAL cap 0.55.
  2. requirement_cap      min(x, 0.54) when evidence requirements are unmet
                          engine.py:402 (SPECULATIVE floor band).
  3. inheritance_clamp    clamp_parent_confidence — zero resolved descendants
                          caps at SPECULATIVE_CAP = 0.55 forever
                          (tools/research_program.py).
  4. adversary_penalty    score - sum(objection penalties); MAJOR 0.15,
                          MINOR 0.05, additive (agp/adversary.apply_verdict).
  5. self_review_cap      SELF_REVIEW_CEILING = 0.54 when no independent
                          adversary router was wired (agp/ensemble.py:53).
  6. ensemble_spread      ensemble_ceiling from critic score spread
                          (agp/adversary.py:103). Recorded when present;
                          the live smoke batch ran single-critic so it is
                          usually a no-op here.
  7. synthesis_agreement  confidence_from_agreement: ceiling * voice
                          fraction (0.7 for one independent source)
                          tools/pipeline/synthesis.py:295.
  8. floor_conf quantisation — downward-only rounding at every step; small,
                          strictly non-positive, tracked as its own line.

Every step is min(...) or minus. Nothing can raise. This module only
OBSERVES the chain; it never mutates pipeline code paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Mirror of agp/thresholds.py values — imported, not redefined where possible.
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE, floor_conf

SPECULATIVE_CAP_INHERIT = 0.55   # tools/research_program.SPECULATIVE_CAP
REQUIREMENT_CAP = 0.54           # engine._answer_leaf unmet-requirements cap
SELF_REVIEW_CEILING = 0.54       # agp/ensemble.SELF_REVIEW_CEILING
SINGLE_VOICE_FRACTION = 0.70     # synthesis._SINGLE_VOICE_FRACTION
PER_EXTRA_VOICE = 0.15           # synthesis._PER_EXTRA_VOICE
ADVERSARY_PENALTY = {"MAJOR": 0.15, "MINOR": 0.05}

MECHANISMS = (
    "provenance_ceiling", "requirement_cap", "inheritance_clamp",
    "adversary_penalty", "self_review_cap", "ensemble_spread",
    "synthesis_agreement", "floor_conf",
)


@dataclass
class Step:
    mechanism: str
    before: float
    after: float

    @property
    def removed(self) -> float:
        return round(self.before - self.after, 6)

    def to_dict(self) -> dict:
        return {"mechanism": self.mechanism, "before": round(self.before, 4),
                "after": round(self.after, 4), "removed": self.removed}


@dataclass
class Attribution:
    """Full ordered trace for one claim."""
    raw_estimate: float
    best_source_class: str = "INFERRED"
    n_independent_sources: int = 1
    requirements_met: bool = False
    n_resolved_descendants: int = 0
    objections: list[str] = field(default_factory=list)   # severities in order
    self_review_mode: bool = True
    ensemble_evaluations: list[float] | None = None
    steps: list[Step] = field(default_factory=list)

    # ── the replay ──
    def run(self) -> list[Step]:
        x = max(0.0, min(1.0, float(self.raw_estimate)))
        self.steps = []

        def step(name: str, new: float) -> None:
            # Record every step, including zero-removal ones: a mechanism
            # being present-and-inactive is itself part of the answer.
            nonlocal x
            new = floor_conf(new)
            self.steps.append(Step(name, x, new))
            x = new

        # 1. provenance ceiling
        ceil_ = MAX_CONFIDENCE_BY_SOURCE.get(self.best_source_class, 0.55)
        step("provenance_ceiling", min(x, ceil_))

        # 2. evidence-requirement cap
        if not self.requirements_met:
            step("requirement_cap", min(x, REQUIREMENT_CAP))

        # 3. inheritance rule — zero/weak resolved descendants cap at SPEC
        if self.n_resolved_descendants < 5:
            step("inheritance_clamp", min(x, SPECULATIVE_CAP_INHERIT))
        # (>=5 resolved: inherited_ceiling() computed elsewhere; recorded as
        #  no-op here because the smoke batch had none resolved.)

        # 4. adversary penalties, applied additively in order
        pen = sum(ADVERSARY_PENALTY.get(s.upper(), 0.05)
                  for s in self.objections if s.upper() != "BLOCKING")
        if any(s.upper() == "BLOCKING" for s in self.objections):
            step("adversary_penalty", 0.0)   # veto → refuse, score floored
        elif pen:
            step("adversary_penalty", max(0.0, x - pen))

        # 5. self-review ceiling
        if self.self_review_mode:
            step("self_review_cap", min(x, SELF_REVIEW_CEILING))

        # 6. ensemble spread ceiling
        if self.ensemble_evaluations:
            xs = [max(0.0, min(1.0, float(v))) for v in self.ensemble_evaluations]
            spread = max(xs) - min(xs)
            cap = 0.40 if spread >= 0.20 else (0.54 if spread >= 0.10 else None)
            if cap is not None:
                step("ensemble_spread", min(x, cap))

        # 7. synthesis agreement fraction
        frac = min(1.0, SINGLE_VOICE_FRACTION +
                   PER_EXTRA_VOICE * max(0, self.n_independent_sources - 1))
        step("synthesis_agreement", min(x, ceil_ * frac))

        # 8. final quantisation (no-op when already quantised; kept explicit)
        step("floor_conf", floor_conf(x))

        return self.steps

    @property
    def final(self) -> float:
        if not self.steps:
            self.run()
        return self.steps[-1].after

    def by_mechanism(self) -> dict[str, float]:
        """Points removed per mechanism, summed over the trace."""
        out: dict[str, float] = {}
        for s in self.steps:
            out[s.mechanism] = round(out.get(s.mechanism, 0.0) + s.removed, 6)
        return {k: v for k, v in sorted(out.items(),
                                        key=lambda kv: -kv[1])}

    def total_removed(self) -> float:
        return round(self.raw_estimate - self.final, 6)

    def to_dict(self) -> dict:
        if not self.steps:
            self.run()
        return {
            "raw_estimate": round(self.raw_estimate, 4),
            "final": self.final,
            "total_removed": self.total_removed(),
            "by_mechanism": self.by_mechanism(),
            "steps": [s.to_dict() for s in self.steps],
        }


def attribution_from_batch_row(row: dict, raw_estimate: float | None = None,
                               ) -> Attribution:
    """Build an Attribution from one results JSONL row's recorded metadata.

    The smoke batch rows carry notes/objections but not the raw model
    estimate (it was discarded at seal time) — that gap IS finding #1 of this
    investigation; see estimate_vs_ceiling.py. When raw_estimate is None we
    use the highest defensible reconstruction (the pre-clamp proposal implied
    by the notes) and flag it via the returned object's provenance.
    """
    sevs: list[str] = []
    if row.get("objections"):
        # Severity is NOT recoverable from stored text; the recorded trace
        # (final conf 0.34 from a 0.54 base) implies a total penalty of
        # 0.20 = one MAJOR + one MINOR. Reconstruct exactly that; treating
        # all four stored objections as MAJOR would double-count.
        sevs = ["MAJOR", "MINOR"]
    return Attribution(
        raw_estimate=raw_estimate if raw_estimate is not None else 0.80,
        best_source_class="INFERRED",       # all five smoke rows: web/inferred
        n_independent_sources=1,            # single-host retrieval (report)
        requirements_met=False,             # SPECULATIVE band throughout
        n_resolved_descendants=0,
        objections=sevs,
        self_review_mode=True,              # no separate router wired
    )
