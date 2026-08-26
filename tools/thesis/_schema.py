"""
Seed schema constants and validation for the Callisto thesis-seed library.

Moved verbatim from tools/thesis_seeds.py; re-exported by that module (and by
tools/thesis/__init__.py) so existing importers keep working.
"""

from __future__ import annotations

REQUIRED_SEED_KEYS = {
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

VALID_CATEGORIES = {
    "props", "totals", "spreads", "h2h", "live", "parlay", "futures",
}

VALID_EXPLORATION = {"unexplored", "partial", "exhausted"}


def validate_seed(seed: dict) -> list[str]:
    """Return a list of validation errors. Empty list = valid."""
    errs: list[str] = []
    if not isinstance(seed, dict):
        return [f"seed is not a dict: {type(seed)}"]
    missing = REQUIRED_SEED_KEYS - set(seed.keys())
    if missing:
        errs.append(f"missing keys: {sorted(missing)}")
    if seed.get("category") not in VALID_CATEGORIES:
        errs.append(f"invalid category: {seed.get('category')}")
    if seed.get("exploration_status") not in VALID_EXPLORATION:
        errs.append(f"invalid exploration_status: {seed.get('exploration_status')}")
    msh = seed.get("min_sample_heuristic")
    if not isinstance(msh, int) or msh <= 0:
        errs.append(f"min_sample_heuristic must be positive int: {msh!r}")
    ic = seed.get("ic_prior_estimate")
    if not isinstance(ic, (int, float)) or not (0.0 <= float(ic) <= 0.5):
        errs.append(f"ic_prior_estimate must be in [0, 0.5]: {ic!r}")
    for k in ("thesis_template", "cohort_filter_sql", "signal_logic",
              "variance_justification", "sport", "market_type"):
        v = seed.get(k, "")
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{k} must be a non-empty string")
    return errs


def _validate_library() -> None:
    """Called at package import time — fail fast on malformed seeds."""
    from . import THESIS_SEEDS

    seen: set[str] = set()
    for s in THESIS_SEEDS:
        errs = validate_seed(s)
        if errs:
            raise ValueError(
                f"Invalid thesis seed {s.get('seed_id', '<missing>')}: {errs}"
            )
        if s["seed_id"] in seen:
            raise ValueError(f"Duplicate seed_id: {s['seed_id']}")
        seen.add(s["seed_id"])
