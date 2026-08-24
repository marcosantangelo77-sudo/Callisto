"""A/B axes: rerun the same questions with each mechanism disabled in turn.

Offline counterfactual arms over recorded runs (no pipeline re-execution —
the live batch costs 43 min/question). Each arm replays the attribution
chain with ONE mechanism neutralised and reports where the points went.

An arm "disables" a mechanism only in the COUNTERFACTUAL REPLAY. Nothing in
agp/ or tools/pipeline/ is touched by this module; other instances own those.
"""
from __future__ import annotations

from dataclasses import replace

from tools.calibration.mechanisms import Attribution, MECHANISMS


def _neutralise(attr: Attribution, mech: str) -> Attribution:
    """Return a copy of attr with `mech`'s inputs set to no-op values."""
    kw: dict = {}
    if mech == "provenance_ceiling":
        kw["best_source_class"] = "PRIMARY"          # ceiling 1.0
    elif mech == "requirement_cap":
        kw["requirements_met"] = True
    elif mech == "inheritance_clamp":
        kw["n_resolved_descendants"] = 99            # past MIN_RESOLVED_FOR_LIFT
    elif mech == "adversary_penalty":
        kw["objections"] = []
    elif mech == "self_review_cap":
        kw["self_review_mode"] = False               # independent review
    elif mech == "ensemble_spread":
        kw["ensemble_evaluations"] = None
    elif mech == "synthesis_agreement":
        # 4 independent sources → frac capped at 1.0 (0.7+3*0.15=1.15→1.0)
        kw["n_independent_sources"] = 4
    elif mech == "floor_conf":
        pass                                          # handled in run loop
    return replace(attr, **kw)


def ab_attribution(attr: Attribution) -> dict:
    """One arm per mechanism: final score with that mechanism removed, and
    how many of the total removed points are attributable to it."""
    if not attr.steps:
        base_steps = attr.run()
    baseline_final = attr.final
    total_removed = attr.total_removed()
    out = {
        "raw_estimate": round(attr.raw_estimate, 4),
        "baseline_final": baseline_final,
        "total_removed": total_removed,
        "arms": {},
    }
    for mech in MECHANISMS:
        a2 = _neutralise(attr, mech)
        a2.run()
        arm_final = a2.final
        out["arms"][mech] = {
            "final_without": arm_final,
            "points_restored": round(arm_final - baseline_final, 6),
        }
    # Normalise: each mechanism's share of the gap it can explain alone.
    for mech, arm in out["arms"].items():
        arm["share_of_gap"] = (
            round(arm["points_restored"] / total_removed, 4)
            if total_removed > 1e-9 else None)
    out["verdict_largest_single"] = max(
        MECHANISMS, key=lambda m: out["arms"][m]["points_restored"])
    return out


def stack_arithmetic(raw_estimate: float = 0.80,
                     best_source_class: str = "INFERRED",
                     n_objections_major: int = 0,
                     n_independent_sources: int = 1) -> dict:
    """Quantify compounding versus each cap alone.

    Independent multiplicative caps compound multiplicatively; additive
    penalties subtract once but act on an already-shrunken base. This shows
    the compounded result against every individual mechanism's own effect.
    """
    from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE, floor_conf
    ceil_ = MAX_CONFIDENCE_BY_SOURCE.get(best_source_class, 0.55)
    x = raw_estimate
    rows = []

    def apply(name, new):
        nonlocal x
        new = floor_conf(new)
        rows.append({"step": name, "value": new,
                     "removed_this_step": round(x - new, 6)})
        x = new

    apply("provenance_ceiling", min(x, ceil_))
    apply("requirement_cap", min(x, 0.54))
    apply("inheritance_clamp", min(x, 0.55))
    pen = n_objections_major * 0.15
    if pen:
        apply("adversary_penalty", max(0.0, x - pen))
    apply("self_review_cap", min(x, 0.54))
    frac = min(1.0, 0.70 + 0.15 * max(0, n_independent_sources - 1))
    apply("synthesis_agreement", min(x, ceil_ * frac))

    # each mechanism ALONE from raw:
    alone = {
        "provenance_ceiling": floor_conf(min(raw_estimate, ceil_)),
        "requirement_cap": floor_conf(min(raw_estimate, 0.54)),
        "inheritance_clamp": floor_conf(min(raw_estimate, 0.55)),
        "adversary_penalty": floor_conf(max(0.0, raw_estimate - pen)),
        "self_review_cap": floor_conf(min(raw_estimate, 0.54)),
        "synthesis_agreement": floor_conf(min(raw_estimate, ceil_ * frac)),
    }
    compounded_removed = round(raw_estimate - x, 6)
    sum_alone_removed = round(sum(raw_estimate - v for v in alone.values()), 6)
    return {
        "raw_estimate": raw_estimate,
        "chain": rows,
        "compounded_final": x,
        "compounded_removed": compounded_removed,
        "each_alone_final": alone,
        "sum_if_naive_additive": sum_alone_removed,
        "compounding_note": (
            "caps compound multiplicatively (min of mins), so the compounded "
            "removal is LESS than the naive sum of single-mechanism removals "
            "— each later cap acts on an already-shrunken base. The bias is "
            "structural: ANY estimate above ~0.34 collapses to the same "
            f"floor ({x})."),
    }
