"""
Tests pinning tools/thesis_seeds.py's public API after the split into
tools/thesis/ (mlb.py, nba.py, nhl.py, nfl.py, misc.py, _schema.py,
runtime.py).

The facade must keep exporting exactly the names downstream code
(tools/hypgen/seeds.py and friends) imports, and the aggregated seed
library must keep its schema keys, uniqueness, validation behavior, and
original ordering.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

thesis_seeds = importlib.import_module("tools.thesis_seeds")


# ───────────────────────────────────────────────────────────────
# Public-name pins on the facade
# ───────────────────────────────────────────────────────────────

PUBLIC_NAMES = [
    "THESIS_SEEDS",
    "MLB_SEEDS",
    "NBA_SEEDS",
    "NHL_SEEDS",
    "NFL_SEEDS",
    "MISC_SEEDS",
    "REQUIRED_SEED_KEYS",
    "VALID_CATEGORIES",
    "VALID_EXPLORATION",
    "validate_seed",
    "list_seeds",
    "get_seed",
    "seed_category_coverage",
    "seed_sport_coverage",
    "pick_unexplored_seeds",
]


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_facade_exports_public_name(name: str) -> None:
    assert hasattr(thesis_seeds, name), f"facade lost public name: {name}"


def test_facade_all_list_is_complete() -> None:
    assert set(thesis_seeds.__all__) == set(PUBLIC_NAMES)


def test_submodules_expose_sport_groups() -> None:
    from tools.thesis import mlb as t_mlb  # noqa: F401
    from tools.thesis import nba as t_nba  # noqa: F401
    from tools.thesis import nhl as t_nhl  # noqa: F401
    from tools.thesis import nfl as t_nfl  # noqa: F401
    from tools.thesis import misc as t_misc  # noqa: F401

    assert thesis_seeds.MLB_SEEDS is t_mlb.MLB_SEEDS
    assert thesis_seeds.NBA_SEEDS is t_nba.NBA_SEEDS
    assert thesis_seeds.NHL_SEEDS is t_nhl.NHL_SEEDS
    assert thesis_seeds.NFL_SEEDS is t_nfl.NFL_SEEDS
    assert thesis_seeds.MISC_SEEDS is t_misc.MISC_SEEDS


def test_thesis_seeds_equals_union_of_groups() -> None:
    union = (
        thesis_seeds.MLB_SEEDS
        + thesis_seeds.NBA_SEEDS
        + thesis_seeds.NHL_SEEDS
        + thesis_seeds.NFL_SEEDS
        + thesis_seeds.MISC_SEEDS
    )
    assert len(union) == len(thesis_seeds.THESIS_SEEDS)
    assert {id(s) for s in union} == {id(s) for s in thesis_seeds.THESIS_SEEDS}


def test_library_size_unchanged() -> None:
    # The pre-split monolith shipped 59 seeds; the split must not drop any.
    assert len(thesis_seeds.THESIS_SEEDS) == 59


def test_group_sizes() -> None:
    assert len(thesis_seeds.MLB_SEEDS) == 23
    assert len(thesis_seeds.NBA_SEEDS) == 15  # NBA + WNBA
    assert len(thesis_seeds.NHL_SEEDS) == 8
    assert len(thesis_seeds.NFL_SEEDS) == 5
    assert len(thesis_seeds.MISC_SEEDS) == 8


def test_group_membership_by_prefix() -> None:
    for s in thesis_seeds.MLB_SEEDS:
        assert s["seed_id"].startswith("mlb_")
    for s in thesis_seeds.NBA_SEEDS:
        assert s["seed_id"].startswith(("nba_", "wnba_"))
    for s in thesis_seeds.NHL_SEEDS:
        assert s["seed_id"].startswith("nhl_")
    for s in thesis_seeds.NFL_SEEDS:
        assert s["seed_id"].startswith("nfl_")


# ───────────────────────────────────────────────────────────────
# Schema pins — every seed keeps the full key set
# ───────────────────────────────────────────────────────────────

def test_required_seed_keys_constant() -> None:
    assert thesis_seeds.REQUIRED_SEED_KEYS == {
        "seed_id",
        "category",
        "sport",
        "market_type",
        "thesis_template",
        "cohort_filter_sql",
        "signal_logic",
        "min_sample_heuristic",
        "ic_prior_estimate",
        "variance_justification",
        "exploration_status",
    }


def test_valid_categories_constant() -> None:
    assert thesis_seeds.VALID_CATEGORIES == {
        "props", "totals", "spreads", "h2h", "live", "parlay", "futures",
    }


def test_valid_exploration_constant() -> None:
    assert thesis_seeds.VALID_EXPLORATION == {"unexplored", "partial", "exhausted"}


def test_every_seed_has_required_keys() -> None:
    for s in thesis_seeds.THESIS_SEEDS:
        missing = thesis_seeds.REQUIRED_SEED_KEYS - set(s.keys())
        assert not missing, f"{s.get('seed_id')} missing keys: {sorted(missing)}"


def test_no_extra_or_empty_schema_fields() -> None:
    required = thesis_seeds.REQUIRED_SEED_KEYS
    for s in thesis_seeds.THESIS_SEEDS:
        sid = s["seed_id"]
        extra = set(s.keys()) - required
        assert not extra, f"{sid} has unexpected keys: {sorted(extra)}"
        for k in (
            "thesis_template", "cohort_filter_sql", "signal_logic",
            "variance_justification", "sport", "market_type",
        ):
            assert isinstance(s[k], str) and s[k].strip(), f"{sid}.{k} empty/non-str"
        assert isinstance(s["min_sample_heuristic"], int)
        assert s["min_sample_heuristic"] > 0, sid
        assert 0.0 <= float(s["ic_prior_estimate"]) <= 0.5, sid


def test_unique_seed_ids_and_valid_fields_import_time() -> None:
    ids = [s["seed_id"] for s in thesis_seeds.THESIS_SEEDS]
    assert len(ids) == len(set(ids))
    for s in thesis_seeds.THESIS_SEEDS:
        assert thesis_seeds.validate_seed(s) == [], s["seed_id"]
        assert s["exploration_status"] in thesis_seeds.VALID_EXPLORATION
        assert s["category"] in thesis_seeds.VALID_CATEGORIES


def test_validate_seed_reports_problems() -> None:
    bad = {
        "seed_id": "x",
        "category": "nope",
        "exploration_status": "maybe",
        "min_sample_heuristic": -1,
        "ic_prior_estimate": 0.9,
    }
    errs = thesis_seeds.validate_seed(bad)
    assert any("missing keys" in e for e in errs)
    assert any("invalid category" in e for e in errs)
    assert any("invalid exploration_status" in e for e in errs)
    assert any("min_sample_heuristic" in e for e in errs)
    assert any("ic_prior_estimate" in e for e in errs)
    assert thesis_seeds.validate_seed("not-a-dict") != []


def test_malformed_library_fails_at_import(tmp_path, monkeypatch):
    """A duplicate seed_id anywhere in the package must raise at import."""
    real = os.path.dirname(importlib.import_module("tools.thesis").__file__)
    import shutil

    shutil.copytree(real, tmp_path / "tools_thesis_copy")
    init = tmp_path / "tools_thesis_copy" / "__init__.py"
    src = init.read_text()
    # Rewrite relative imports so the copied tree is self-contained under a
    # synthetic top-level package name.
    src = (
        src.replace(
            'assert set(_by_id) == set(_ORIG_ORDER), "seed-id drift vs original library"',
            '# drift check disabled for duplicate-injection test',
        )
        .replace('THESIS_SEEDS[:] = [_by_id[sid] for sid in _ORIG_ORDER]', '')
        .replace('from ._schema import', 'from tools_thesis_copy._schema import')
           .replace('from .runtime import', 'from tools_thesis_copy.runtime import')
           .replace('from .mlb import MLB_SEEDS', 'from tools_thesis_copy.mlb import MLB_SEEDS')
           .replace('from .nba import NBA_SEEDS', 'from tools_thesis_copy.nba import NBA_SEEDS')
           .replace('from .nhl import NHL_SEEDS', 'from tools_thesis_copy.nhl import NHL_SEEDS')
           .replace('from .nfl import NFL_SEEDS', 'from tools_thesis_copy.nfl import NFL_SEEDS')
           .replace('from .misc import MISC_SEEDS', 'from tools_thesis_copy.misc import MISC_SEEDS')
    )
    init.write_text(src)
    for sub in ("_schema", "runtime"):
        p = tmp_path / "tools_thesis_copy" / f"{sub}.py"
        p.write_text(
            p.read_text().replace("from . import THESIS_SEEDS",
                                  "from tools_thesis_copy import THESIS_SEEDS")
        )
    mlb = tmp_path / "tools_thesis_copy" / "mlb.py"
    src = mlb.read_text().replace(
        '"mlb_umpire_k_prop_bias"', '"mlb_umpire_zone_totals_bias"'
    )
    mlb.write_text(src)

    # Load as a synthetic top-level package so its relative imports resolve
    # to the copied (mutated) tree, not the real tools.thesis package.
    sys.path.insert(0, str(tmp_path))
    sys.modules.pop("tools_thesis_copy", None)
    try:
        with pytest.raises(ValueError, match="Duplicate seed_id"):
            importlib.import_module("tools_thesis_copy")
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("tools_thesis_copy", None)


# ───────────────────────────────────────────────────────────────
# Content parity with the pre-split monolith
# ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def original_module():
    """Load the pre-split thesis_seeds.py from git HEAD without touching
    the working tree."""
    import subprocess

    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    blob = subprocess.run(
        ["git", "-C", repo, "show", "HEAD:tools/thesis_seeds.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    name = "_pre_split_thesis_seeds"
    spec = importlib.util.spec_from_file_location(name, "/tmp/orig_thesis_seeds_test.py")
    mod = importlib.util.module_from_spec(spec)
    with open("/tmp/orig_thesis_seeds_test.py", "w") as f:
        f.write(blob)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_seed_dicts_identical_to_pre_split(original_module) -> None:
    old = {s["seed_id"]: s for s in original_module.THESIS_SEEDS}
    new = {s["seed_id"]: s for s in thesis_seeds.THESIS_SEEDS}
    assert set(old) == set(new)
    for sid, seed in old.items():
        assert seed == new[sid], f"seed drifted during split: {sid}"


def test_library_order_preserved(original_module) -> None:
    old_order = [s["seed_id"] for s in original_module.THESIS_SEEDS]
    new_order = [s["seed_id"] for s in thesis_seeds.THESIS_SEEDS]
    assert old_order == new_order, "library order changed; pick_unexplored_seeds is order-sensitive"


# ───────────────────────────────────────────────────────────────
# Runtime query helpers behave identically through the facade
# ───────────────────────────────────────────────────────────────

def test_list_seeds_filters() -> None:
    all_ids = {s["seed_id"] for s in thesis_seeds.list_seeds()}
    mlb_ids = {s["seed_id"] for s in thesis_seeds.list_seeds(sport="baseball_mlb")}
    totals_ids = {s["seed_id"] for s in thesis_seeds.list_seeds(category="totals")}
    unexplored = {
        s["seed_id"]
        for s in thesis_seeds.list_seeds(exploration_status="unexplored")
    }
    assert all_ids == {s["seed_id"] for s in thesis_seeds.THESIS_SEEDS}
    assert mlb_ids < all_ids and len(mlb_ids) > 0
    assert all(s.startswith("mlb_") for s in mlb_ids)
    assert totals_ids <= all_ids
    combined = thesis_seeds.list_seeds(
        sport="baseball_mlb", category="totals", exploration_status="unexplored"
    )
    assert {s["seed_id"] for s in combined} <= (mlb_ids & totals_ids & unexplored)


def test_get_seed_roundtrip() -> None:
    seed = thesis_seeds.get_seed("mlb_umpire_zone_totals_bias")
    assert seed is not None and seed["seed_id"] == "mlb_umpire_zone_totals_bias"
    assert seed["sport"] == "baseball_mlb"
    assert thesis_seeds.get_seed("does_not_exist_xyz") is None


def test_coverage_helpers_consistent() -> None:
    cat = thesis_seeds.seed_category_coverage()
    sport = thesis_seeds.seed_sport_coverage()
    assert sum(cat.values()) == 59
    assert sum(sport.values()) == 59
    assert set(cat) <= thesis_seeds.VALID_CATEGORIES
    for s in thesis_seeds.THESIS_SEEDS:
        assert cat[s["category"]] >= 1
        assert sport[s["sport"]] >= 1


def test_pick_unexplored_seeds_matches_pre_split_behavior(original_module) -> None:
    existing = ["hypothesis about mlb_umpire_zone_totals_bias thing"]
    new_pick = thesis_seeds.pick_unexplored_seeds(existing, max_seeds=3)
    old_pick = original_module.pick_unexplored_seeds(existing, max_seeds=3)
    assert [s["seed_id"] for s in new_pick] == [s["seed_id"] for s in old_pick]

    plain_new = thesis_seeds.pick_unexplored_seeds([], max_seeds=5)
    plain_old = original_module.pick_unexplored_seeds([], max_seeds=5)
    assert [s["seed_id"] for s in plain_new] == [s["seed_id"] for s in plain_old]

    # keyword-overlap skip path
    theses = [
        "umpire zone totals bias drives scoring in called-strike-heavy games"
    ]
    kw_new = thesis_seeds.pick_unexplored_seeds([], theses, max_seeds=10)
    kw_old = original_module.pick_unexplored_seeds([], theses, max_seeds=10)
    assert [s["seed_id"] for s in kw_new] == [s["seed_id"] for s in kw_old]
    assert "mlb_umpire_zone_totals_bias" not in [s["seed_id"] for s in kw_new]


def test_downstream_hypgen_still_imports() -> None:
    mod = importlib.import_module("tools.hypgen.seeds")
    assert mod is not None


def test_no_live_status_leak() -> None:
    """Guardrail: seeds never carry a 'live' exploration status and no
    category named exactly 'live' implies live betting arming."""
    for s in thesis_seeds.THESIS_SEEDS:
        assert s["exploration_status"] != "live"
