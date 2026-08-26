"""
Runtime query helpers over the aggregated thesis-seed library.

Moved verbatim from tools/thesis_seeds.py; re-exported by that module so
existing importers (e.g. tools/hypgen/seeds.py) keep working.
"""

from __future__ import annotations

from typing import Iterable, Optional


def list_seeds(
    sport: Optional[str] = None,
    category: Optional[str] = None,
    exploration_status: Optional[str] = None,
) -> list[dict]:
    """Filtered view of the seed library."""
    from . import THESIS_SEEDS

    out = list(THESIS_SEEDS)
    if sport:
        out = [s for s in out if s["sport"] == sport]
    if category:
        out = [s for s in out if s["category"] == category]
    if exploration_status:
        out = [s for s in out if s["exploration_status"] == exploration_status]
    return out


def get_seed(seed_id: str) -> Optional[dict]:
    from . import THESIS_SEEDS

    for s in THESIS_SEEDS:
        if s["seed_id"] == seed_id:
            return s
    return None


def seed_category_coverage() -> dict[str, int]:
    """Map category → count for dashboarding."""
    from . import THESIS_SEEDS

    counts: dict[str, int] = {}
    for s in THESIS_SEEDS:
        counts[s["category"]] = counts.get(s["category"], 0) + 1
    return counts


def seed_sport_coverage() -> dict[str, int]:
    from . import THESIS_SEEDS

    counts: dict[str, int] = {}
    for s in THESIS_SEEDS:
        counts[s["sport"]] = counts.get(s["sport"], 0) + 1
    return counts


def pick_unexplored_seeds(
    existing_hypothesis_names: Iterable[str],
    existing_thesis_statements: Iterable[str] = (),
    sport: Optional[str] = None,
    max_seeds: int = 5,
) -> list[dict]:
    """Return up to ``max_seeds`` seeds whose ``seed_id`` does NOT appear
    in any existing hypothesis name or notes — a cheap keyword filter.

    Semantic near-dup check happens later in the generator; this is the
    coarse-grain pass so the LLM doesn't get asked to re-specialize a seed
    that's already been exhausted.
    """
    existing_names_l = [n.lower() for n in existing_hypothesis_names]
    existing_theses_l = [t.lower() for t in existing_thesis_statements]
    pool = list_seeds(sport=sport)
    picked: list[dict] = []
    for s in pool:
        if len(picked) >= max_seeds:
            break
        sid_l = s["seed_id"].lower()
        if any(sid_l in n for n in existing_names_l):
            continue
        # Cheap keyword overlap: skip if a distinctive seed-id token shows
        # up in an existing thesis body verbatim.
        distinctive = [tok for tok in sid_l.split("_") if len(tok) > 4]
        if distinctive and any(
            all(tok in t for tok in distinctive) for t in existing_theses_l
        ):
            continue
        picked.append(s)
    return picked
