"""Hand-curated mutation catalog.

Each mutation is a small, realistic defect: a flipped comparison, an inverted
boundary, a dropped clamp, a negated penalty, a skipped verification. Anchors
must occur exactly once in their module. Mutations are applied to COPIES only.
"""

MUTATIONS: dict[str, dict] = {
    # ──────────────────────────────────────────────────────── thresholds ──
    "agp/thresholds.py": {
        "tests": ["tests/test_agp.py",
                  "tests/test_lifecycle_seal.py",
                  "tests/test_tier0_money_kelly.py"],
        "mutations": [
            {"name": "verified_min_lowered_090_to_085",
             "old": "TIER_VERIFIED_MIN = 0.90",
             "new": "TIER_VERIFIED_MIN = 0.85"},
            {"name": "secondary_ceiling_raised_075_to_080",
             "old": '"SECONDARY": 0.75,',
             "new": '"SECONDARY": 0.80,'},
            {"name": "critical_contradiction_penalty_zeroed",
             "old": '"CRITICAL": 0.15,',
             "new": '"CRITICAL": 0.00,'},
            {"name": "db_floor_lowered_030_to_010",
             "old": "DB_CONFIDENCE_FLOOR = 0.30",
             "new": "DB_CONFIDENCE_FLOOR = 0.10"},
        ],
    },
    # ─────────────────────────────────────────────────────── provenance ──
    "agp/provenance.py": {
        "tests": ["tests/test_tier3_epi_provenance.py",
                  "tests/test_lifecycle_seal.py",
                  "tests/test_build_w1_retrieval.py"],
        "mutations": [
            {"name": "primary_check_skipped",
             # primary bytes demoted to secondary: the ledger stops
             # distinguishing fetched-document bytes from search results
             "old": """        if self.is_primary_bytes(content):
            return SourceClass.PRIMARY
        if self.has_observation(content):""",
             "new": """        if False and self.is_primary_bytes(content):
            return SourceClass.PRIMARY
        if self.has_observation(content):"""},
            {"name": "cites_verified_url_inverted",
             "old": "        return any(u in self._urls for u in extract_urls(text))",
             "new": "        return not any(u in self._urls for u in extract_urls(text))"},
            {"name": "clamp_confidence_drops_ceiling_cap",
             "old": "    return round(min(score, max_by_source.get(source_class.value, 0.55)), 2)",
             "new": "    return round(score, 2)"},
            {"name": "relabel_demotion_count_zeroed",
             "old": "            demoted += 1",
             "new": "            pass"},
            {"name": "relabel_rank_comparison_flipped",
             "old": "        if rank.get(assigned.value, 0) < rank.get(ev.source_class.value, 0):",
             "new": "        if rank.get(assigned.value, 0) > rank.get(ev.source_class.value, 0):"},
        ],
    },
    # ──────────────────────────────────────────────────────── adversary ──
    "agp/adversary.py": {
        "tests": ["tests/test_build_r3_adversary.py",
                  "tests/test_build_gaps.py",
                  "tests/test_build_w4_cross_model.py"],
        "mutations": [
            {"name": "blocking_objection_ignored",
             # a failed check treated as passed: BLOCKING no longer vetoes
             "old": "        for ob in objs:\n            if ob.is_blocking:",
             "new": "        for ob in []:\n            if ob.is_blocking:"},
            {"name": "objection_penalties_negated",
             "old": '{"BLOCKING": 0.0, "MAJOR": 0.15, "MINOR": 0.05}',
             "new": '{"BLOCKING": 0.0, "MAJOR": -0.15, "MINOR": -0.05}'},
            {"name": "backend_failure_fails_open",
             # router crash no longer produces the fail-closed BLOCKING objection
             "old": """        except Exception as e:  # noqa: BLE001 — fail closed by design
            return [AdversaryObjection(""",
             "new": """        except Exception as e:  # noqa: BLE001 — fail closed by design
            return []
        if False:
            return [AdversaryObjection(""",
             },
            {"name": "ensemble_spread_threshold_widened",
             "old": "DISAGREEMENT_SPREAD_THRESHOLD = 0.30",
             "new": "DISAGREEMENT_SPREAD_THRESHOLD = 0.90"},
            {"name": "clamp_with_ensemble_returns_score_when_above_ceiling",
             "old": "    if ceil_ is None or s <= ceil_:",
             "new": "    if ceil_ is None or s <= ceil_ or True:"},
        ],
    },
    # ──────────────────────────────────────────────────────── ensemble ──
    "agp/ensemble.py": {
        "tests": ["tests/test_build_w4_cross_model.py",
                  "tests/test_build_r3_adversary.py"],
        "mutations": [
            {"name": "self_review_ceiling_widened_to_corroborated",
             "old": "SELF_REVIEW_CEILING = TIER_SPECULATIVE_MAX",
             "new": "SELF_REVIEW_CEILING = 0.75"},
            {"name": "unanimity_penalty_zeroed",
             "old": "UNANIMITY_BONUS_PENALTY = 0.10",
             "new": "UNANIMITY_BONUS_PENALTY = 0.00"},
            {"name": "independent_review_ambiguity_resolves_liberal",
             # unknown reviewer now counts as evidence of independence
             "old": """        return any(m not in ("", "(unattributed)")
                   and normalize_model(m) != a for m in self.reviewer_models)""",
             "new": """        return any(normalize_model(m) != a for m in self.reviewer_models)"""},
            {"name": "panel_provenance_ceiling_skipped",
             "old": "        if ceil_ is not None and clamped > ceil_:",
             "new": "        if False and ceil_ is not None and clamped > ceil_:"},
            {"name": "unanimity_requires_all_but_one",
             # family-collapse style: near-unanimity reads as unanimity's opposite
             "old": "        return bool(indep) and all(m in attackers for m in indep)",
             "new": "        return bool(indep) and any(m in attackers for m in indep)"},
        ],
    },
    # ──────────────────────────────────────────────── research_program ──
    "tools/research_program.py": {
        "tests": ["tests/test_build_b4_inheritance.py",
                  "tests/test_build_b4_research_program.py",
                  "tests/test_build_r1_scoring.py",
                  "tests/test_build_p2_claims.py"],
        "mutations": [
            {"name": "min_resolved_for_lift_lowered_5_to_2",
             "old": "MIN_RESOLVED_FOR_LIFT = 5",
             "new": "MIN_RESOLVED_FOR_LIFT = 2"},
            {"name": "wilson_z_zeroed_no_confidence_discount",
             "old": "_WILSON_Z = 1.645",
             "new": "_WILSON_Z = 0.0"},
            {"name": "stale_penalty_negated",
             "old": "    penalty = 0.20 * tr.stale_fraction",
             "new": "    penalty = -0.20 * tr.stale_fraction"},
            {"name": "brier_calibration_inverted",
             "old": "    calib = max(0.0, 1.0 - 2.0 * tr.brier)",
             "new": "    calib = max(0.0, 2.0 * tr.brier - 1.0)"},
            {"name": "source_class_cap_skipped",
             "old": "    return round(min(score, src_cap), 4)",
             "new": "    return round(score, 4)"},
            {"name": "clamp_parent_returns_raw_score",
             "old": "    clamped = round(min(raw, ceil_), 2)",
             "new": "    clamped = round(raw, 2)"},
        ],
    },
    # ─────────────────────────────────────────────────────── synthesis ──
    "tools/pipeline/synthesis.py": {
        "tests": ["tests/test_build_i3_synthesis.py",
                  "tests/test_build_r2_seams.py"],
        "mutations": [
            {"name": "contradiction_cap_widened_to_probable",
             "old": "_SPECULATIVE_CAP = 0.54",
             "new": "_SPECULATIVE_CAP = 0.70"},
            {"name": "class_rank_demotes_nothing",
             "old": '_CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}',
             "new": '_CLASS_RANK = {"INFERRED": 3, "SIGNAL": 3, "SECONDARY": 3, "PRIMARY": 3}'},
        ],
    },
    # ─────────────────────────────────────────────────────── retrieval ──
    "tools/pipeline/retrieval.py": {
        "tests": ["tests/test_build_w1_retrieval.py",
                  "tests/test_build_i3_synthesis.py",
                  "tests/test_build_i2_routable_coverage.py"],
        "mutations": [
            {"name": "relevance_gate_admits_everything",
             "old": "        if coverage < self.min_coverage:",
             "new": "        if coverage < 0.0:"},
            {"name": "independence_family_collapse_disabled",
             # family collapse returns the host instead: dependent sources
             # count as independent again
             "old": """    for family, members in _OVERLAP_FAMILIES.items():
        if any(_norm(m) == nname for m in members):
            return family""",
             "new": """    for family, members in []:  # families ignored
        if any(_norm(m) == nname for m in members):
            return family"""},
            {"name": "min_independent_comparison_flipped",
             "old": "            sufficient = len(trace.independent_keys) >= min_independent",
             "new": "            sufficient = len(trace.independent_keys) >= 1 or min_independent <= 0"},
            {"name": "rejected_items_not_recorded",
             "old": "                if not ok:",
             "new": "                if not ok and False:"},
        ],
    },
    # ───────────────────────────────────────────────────────────── edge ──
    "tools/edge.py": {
        "tests": ["tests/test_build_r5_edge.py",
                  "tests/test_tier0_money_kelly.py",
                  "tests/test_clv_paper_trades.py"],
        "mutations": [
            {"name": "kelly_cap_widened_quarter_to_full",
             "old": "MAX_FRACTION_FULL_KELLY = 0.25",
             "new": "MAX_FRACTION_FULL_KELLY = 1.00"},
            {"name": "kelly_cap_dropped",
             "old": "    kelly_full_capped = min(kelly_full_frac, MAX_FRACTION_FULL_KELLY)",
             "new": "    kelly_full_capped = kelly_full_frac"},
            {"name": "edge_uses_raw_implied_not_devigged",
             # reintroduce the phantom-edge bug
             "old": "    edge = calibrated_prob - market_fair",
             "new": "    edge = calibrated_prob - market_raw"},
            {"name": "min_edge_comparison_flipped",
             "old": "    actionable = edge >= min_edge and ev_per_unit > 0",
             "new": "    actionable = edge > min_edge or ev_per_unit > 0"},
            {"name": "clv_requires_no_devig",
             # grade against un-devigged quotes: phantom CLV returns
             "old": """    if not (a_claim.get("devigged") and a_close.get("devigged")):
        return None""",
             "new": """    if a_claim.get("devigged") and a_close.get("devigged"):
        return None"""},
            {"name": "kelly_floor_removed_negative_fractions_possible",
             "old": "    kelly_full_frac = max(0.0, (b * calibrated_prob - q) / b)",
             "new": "    kelly_full_frac = (b * calibrated_prob - q) / b"},
        ],
    },
    # ──────────────────────────────────────────────────────────── kelly ──
    "tools/kelly.py": {
        "tests": ["tests/test_tier0_money_kelly.py",
                  "tests/test_tier0_money_sizing_and_units.py",
                  "tests/test_portfolio_sizing.py",
                  "tests/test_bankroll_sim.py",
                  "tests/test_regime_sizing.py"],
        "mutations": [
            {"name": "kelly_negative_fraction_not_floored",
             "old": "    return max(0.0, round(fraction, 6))",
             "new": "    return round(fraction, 6)"},
            {"name": "unverified_tier_still_bets",
             "old": '    "UNVERIFIED":    0.00,   # <  0.30: do not bet',
             "new": '    "UNVERIFIED":    0.30,   # <  0.30: do not bet'},
            {"name": "hard_cap_5pct_removed",
             "old": "    hard_cap = 0.05",
             "new": "    hard_cap = 1.00"},
            {"name": "variance_dampener_inverted",
             "old": "    variance_dampener = 1.0 / (1.0 + k * variance_estimate)",
             "new": "    variance_dampener = 1.0 / (1.0 - k * variance_estimate)"},
        ],
    },
    # ──────────────────────────────────────────────────────── hypothesis ──
    "tools/hypothesis.py": {
        "tests": ["tests/test_hypothesis.py",
                  "tests/test_promotion_gates.py",
                  "tests/test_adaptive_timeout.py",
                  "tests/test_sidak_denominator.py"],
        "mutations": [
            {"name": "adaptive_threshold_relaxes_large_n",
             "old": "        if n_signals < 8:\n            return 0.30",
             "new": "        if n_signals < 80:\n            return 0.30"},
            {"name": "min_days_gate_comparison_flipped",
             "old": "            elif days_in_stage < gate[\"min_days\"]:",
             "new": "            elif days_in_stage > gate[\"min_days\"]:"},
            {"name": "min_paper_trades_gate_never_fails",
             "old": "            if resolved_paper_trades < required_trades:",
             "new": "            if False and resolved_paper_trades < required_trades:"},
            {"name": "binomial_pvalue_always_significant",
             "old": "    if wins <= 0:\n        return 1.0",
             "new": "    if wins <= 0:\n        return 0.0"},
        ],
    },
}
