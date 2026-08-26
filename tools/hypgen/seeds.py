"""
Seed selection helpers for the wiki-grounded hypothesis generator.

Extracted from tools/hypothesis_generator.py as part of the hypgen split.
Thin wrapper over tools.thesis_seeds.pick_unexplored_seeds — kept separate
so prompt assembly (tools.hypgen.prompts) stays free of seed logic.
"""

import logging

logger = logging.getLogger("callisto.hypgen.seeds")


def pick_unexplored_seeds(
    existing_names: set,
    existing_theses: list,
    sport: str,
    max_seeds: int = 3,
) -> list:
    """Pick underexplored thesis seeds for a sport. Non-fatal on failure."""
    try:
        from tools.thesis_seeds import pick_unexplored_seeds as _pick
        return _pick(existing_names, existing_theses, sport=sport, max_seeds=max_seeds)
    except Exception as e:
        logger.debug(f"Seed retrieval failed (non-fatal): {e}")
        return []
