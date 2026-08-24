"""RTR — the routing-facing report over measured (role, model) scores.

A routing decision made on 3 observations must be VISIBLY different from one
made on 300. This module renders exactly that: per model, per role — honest
(effective) sample counts beside raw ones, mean Brier raw and shrinkage-
adjusted, a calibration table, and the readiness verdict that decides whether
`empirical_routing.enabled` may flip.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tools.routing.readiness import (
    MIN_DETECTABLE_BRIER,
    PAIRWISE_MIN_N,
    RoleReadiness,
    readiness_report,
    role_readiness,
)


def _calibration_table(records: list[dict], n_bins: int = 5) -> list[dict]:
    """Binned calibration from stored (predicted_probability, answer_binary).
    Records missing either field are skipped — never imputed."""
    rows = [r for r in records
            if r.get("predicted_probability") is not None
            and r.get("answer_binary") is not None]
    width = 1.0 / n_bins
    out = []
    for i in range(n_bins):
        lo, hi = i * width, (i + 1) * width
        bucket = [(r["predicted_probability"],
                   1.0 if r["answer_binary"] else 0.0)
                  for r in rows
                  if lo <= r["predicted_probability"] < hi
                  or (i == n_bins - 1 and r["predicted_probability"] == 1.0)]
        entry = {"bin_low": round(lo, 2), "bin_high": round(hi, 2),
                 "n": len(bucket), "mean_p": None, "realised": None}
        if bucket:
            entry["mean_p"] = round(sum(p for p, _ in bucket)
                                    / len(bucket), 4)
            entry["realised"] = round(sum(y for _, y in bucket)
                                      / len(bucket), 4)
        out.append(entry)
    return out


def build_routing_report(score_store, *,
                         attributions_by_role: dict[str, dict[str, list]]
                         | None = None,
                         candidates_by_role: dict[str, list[str]]
                         | None = None) -> dict:
    """Full report over the store.

    `attributions_by_role`: {role: {model: [RunAttribution]}} when available
    (from batch runs) — enables honest effective counts. Without it, raw
    record counts stand in and are labelled as such.
    `candidates_by_role`: {role: [model names]} from providers.yaml — roles
    with candidates are assessed for readiness; others are reported only.
    """
    attributions_by_role = attributions_by_role or {}
    candidates_by_role = candidates_by_role or {}

    all_records = score_store.load_all()
    roles: dict[str, list[dict]] = {}
    for rec in all_records:
        roles.setdefault(rec.get("role", "?"), []).append(rec)

    role_sections: dict[str, dict] = {}
    readiness_list: list[RoleReadiness] = []
    for role, records in sorted(roles.items()):
        by_model: dict[str, list[dict]] = {}
        for rec in records:
            by_model.setdefault(rec.get("model", "?"), []).append(rec)

        # Honest counts where run attributions exist.
        attrs = attributions_by_role.get(role, {})
        honest_counts: dict[str, int] | None = None
        if attrs:
            from tools.retrodiction.attribution import \
                effective_observation_count
            honest_counts = {m: effective_observation_count(a, m)
                             for m, a in attrs.items()}

        models_section = {}
        for model, recs in sorted(by_model.items()):
            agg = score_store.aggregate(recs)
            raw_n = agg.pop("n")
            honest_n = (honest_counts or {}).get(model, raw_n)
            inflated = honest_n < raw_n
            models_section[model] = {
                "n_raw": raw_n,
                "n_honest": honest_n,
                "counts_inflated_by_correlation": inflated,
                "mean_brier_raw": agg["mean_brier_raw"],
                "mean_brier_shrunk": agg["mean_brier"],
                "mean_cost_usd": agg["mean_cost_usd"],
                "total_cost_usd": agg["total_cost_usd"],
                "basis": score_store.basis_label(honest_n),
                "last_recorded_at": agg["last_recorded_at"],
                "calibration": _calibration_table(recs),
            }

        rr = role_readiness(
            role,
            {m: attrs.get(m, []) for m in by_model} if attrs else
            {m: [] for m in by_model},
            candidates=candidates_by_role.get(role),
            honest_counts_override=(None if attrs else
                                    {m: len(by_model[m]) for m in by_model}))
        if not attrs:
            # no attributions: every record treated as one observation; the
            # report labels this so the reader knows correlation was NOT
            # adjusted for.
            rr.honest_counts = {m: len(by_model[m]) for m in by_model}
        readiness_list.append(rr)

        role_sections[role] = {
            "n_records": len(records),
            "models": models_section,
            "readiness": rr.to_dict(),
        }

    overall = readiness_report(readiness_list)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_records": len(all_records),
        "thresholds": {
            "pairwise_min_n_per_model": PAIRWISE_MIN_N,
            "min_detectable_brier": MIN_DETECTABLE_BRIER,
            "basis_labels": {label: thr for thr, label
                             in reversed(score_store.BASIS_THRESHOLDS)},
        },
        "roles": role_sections,
        "readiness": overall,
        "empirical_routing_enabled_now": overall[
            "empirical_routing_should_enable"],
    }


def render_routing_report(report: dict) -> str:
    """Readable rendering — sample size is impossible to miss."""
    L = []
    L.append("=" * 68)
    L.append("EMPIRICAL ROUTING REPORT — retrodiction-scored models")
    L.append(f"generated {report['generated_at']}")
    L.append("=" * 68)
    th = report["thresholds"]
    L.append(f"trust threshold: >= {th['pairwise_min_n_per_model']} honest "
             f"observations PER MODEL to rank two models head-to-head "
             f"(detect Δ Brier ≥ {th['min_detectable_brier']}, 95%/80%)")
    L.append("")
    for role, sec in report["roles"].items():
        rd = sec["readiness"]
        L.append(f"-- ROLE: {role}  ({sec['n_records']} records, "
                 f"ready={rd['ready']}) --")
        hdr = (f"{'model':<26}{'n_honest':>9}{'n_raw':>7}{'brier':>8}"
               f"{'shrunk':>8}{'basis':>13}")
        L.append(hdr)
        for model, m in sec["models"].items():
            flag = " *" if m["counts_inflated_by_correlation"] else ""
            L.append(f"{model[:25]:<26}{m['n_honest']:>9}{m['n_raw']:>7}"
                     f"{m['mean_brier_raw']:>8}{m['mean_brier_shrunk']:>8}"
                     f"{m['basis']:>13}{flag}")
        if any(m["counts_inflated_by_correlation"]
               for m in sec["models"].values()):
            L.append("  * one model played several roles on shared runs — "
                     "honest count < raw count (correlated, counted once "
                     "per question)")
        live = []
        for m in sec["models"].values():
            live.extend(b for b in m["calibration"] if b["n"])
        if live:
            L.append(f"  {'bin':<16}{'n':>5}{'mean_p':>9}{'realised':>9}")
            seen_bins: dict[str, dict] = {}
            for m in sec["models"].values():
                pass
            for model, m in sec["models"].items():
                for b in m["calibration"]:
                    if not b["n"]:
                        continue
                    key = f"[{b['bin_low']:.1f},{b['bin_high']:.1f})"
                    row = seen_bins.setdefault(
                        key, {"n": 0, "p": [], "y": []})
                    row["n"] += b["n"]
                    row["p"].append(b["mean_p"])
                    row["y"].append(b["realised"])
            for key, row in sorted(seen_bins.items()):
                mp = sum(row["p"]) / len(row["p"])
                mr = sum(row["y"]) / len(row["y"])
                L.append(f"  {key:<16}{row['n']:>5}{mp:>9.3f}{mr:>9.3f}")
        if rd.get("blocking_reason"):
            L.append(f"  BLOCKED: {rd['blocking_reason']}")
        L.append("")
    ov = report["readiness"]
    L.append("VERDICT: " + (
        "ALL routed roles measured — empirical routing MAY be enabled"
        if ov["all_roles_ready"] else
        "empirical_routing stays DISABLED"))
    for b in ov["blockers"]:
        L.append(f"  blocker [{b['role']}]: {b['reason']}")
    L.append("")
    L.append(ov["rationale"])
    return "\n".join(L)
