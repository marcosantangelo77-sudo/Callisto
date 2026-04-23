"""Unit tests for the thesis-seed library.

Non-LLM, non-DB — just exercises the schema validator and runtime helpers.
"""

from __future__ import annotations

import pytest

from tools.thesis_seeds import (
    THESIS_SEEDS,
    REQUIRED_SEED_KEYS,
    VALID_CATEGORIES,
    VALID_EXPLORATION,
    validate_seed,
    list_seeds,
    get_seed,
    seed_category_coverage,
    seed_sport_coverage,
    pick_unexplored_seeds,
)


def test_library_has_minimum_50_entries():
    assert len(THESIS_SEEDS) >= 50, (
        f"thesis seed library has {len(THESIS_SEEDS)} entries; "
        f"spec requires >= 50"
    )


def test_all_seeds_validate():
    for seed in THESIS_SEEDS:
        errs = validate_seed(seed)
        assert errs == [], f"Seed {seed.get('seed_id')} failed: {errs}"


def test_seed_ids_unique():
    ids = [s["seed_id"] for s in THESIS_SEEDS]
    assert len(ids) == len(set(ids)), "duplicate seed_id detected"


def test_seed_required_keys_enforced():
    # Copy of a real seed with one key removed → must fail validation.
    broken = dict(THESIS_SEEDS[0])
    broken.pop("ic_prior_estimate")
    errs = validate_seed(broken)
    assert any("ic_prior_estimate" in e for e in errs)


def test_seed_invalid_category_rejected():
    broken = dict(THESIS_SEEDS[0])
    broken["category"] = "not_a_real_category"
    errs = validate_seed(broken)
    assert any("category" in e for e in errs)


def test_seed_ic_prior_bounds():
    broken = dict(THESIS_SEEDS[0])
    broken["ic_prior_estimate"] = 1.7  # > 0.5 ceiling
    errs = validate_seed(broken)
    assert any("ic_prior_estimate" in e for e in errs)


def test_category_coverage_spans_multiple_families():
    cov = seed_category_coverage()
    # At least 4 distinct categories across {props, totals, spreads, h2h,
    # live, parlay, futures}.
    assert len(cov) >= 4, f"category coverage too narrow: {cov}"


def test_sport_coverage_spans_5_plus_sports():
    cov = seed_sport_coverage()
    assert len(cov) >= 5, f"sport coverage too narrow: {cov}"


def test_list_seeds_filter_by_sport():
    mlb = list_seeds(sport="baseball_mlb")
    assert mlb, "expected at least one MLB seed"
    assert all(s["sport"] == "baseball_mlb" for s in mlb)


def test_list_seeds_filter_by_category():
    live = list_seeds(category="live")
    assert live, "expected at least one live seed"
    assert all(s["category"] == "live" for s in live)


def test_get_seed_roundtrip():
    sid = THESIS_SEEDS[0]["seed_id"]
    assert get_seed(sid)["seed_id"] == sid
    assert get_seed("does_not_exist_zzz") is None


def test_pick_unexplored_skips_already_used():
    # Pick with an empty exclusion list — should return items.
    seeds = pick_unexplored_seeds([], [], sport="baseball_mlb", max_seeds=3)
    assert seeds, "expected at least one MLB seed from unfiltered pool"
    # Now put one seed's id into the existing names and verify it gets filtered.
    first_id = seeds[0]["seed_id"]
    seeds_after = pick_unexplored_seeds(
        [f"some_hypothesis_using_{first_id}_somewhere"],
        [], sport="baseball_mlb", max_seeds=3,
    )
    assert all(s["seed_id"] != first_id for s in seeds_after)


def test_valid_constants_shape():
    # Guard against accidental edits to the validator constants.
    assert "seed_id" in REQUIRED_SEED_KEYS
    assert "thesis_template" in REQUIRED_SEED_KEYS
    assert "totals" in VALID_CATEGORIES
    assert "live" in VALID_CATEGORIES
    assert "unexplored" in VALID_EXPLORATION
