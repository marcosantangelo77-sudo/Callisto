"""
Runtime prop-category discovery for MLB/NHL via Nash name-pattern matching.
"""
import logging
from typing import Optional

from tools.dkscrape.client import _nash_get
from tools.dkscrape.constants import (
    _NASH_BASE,
    DK_PROP_CATEGORIES,
    DK_PROP_NAME_PATTERNS,
    LEAGUE_IDS,
)

logger = logging.getLogger("callisto.dk_scraper")

# Cache: sport -> {prop_key: cat_id}. Resolved lazily on first use.
_prop_category_cache: dict[str, dict[str, int]] = {}


async def discover_prop_categories(sport: str) -> dict[str, int]:
    """
    Inspect the DK Nash eventgroup response for ``sport`` and match
    offer-category / subcategory names against DK_PROP_NAME_PATTERNS.

    Returns {prop_key: resolved_category_id}. Missing entries mean DK
    did not expose that category today (no games, or market temporarily
    unavailable) — callers should degrade gracefully.
    """
    if sport in _prop_category_cache:
        return _prop_category_cache[sport]

    patterns = DK_PROP_NAME_PATTERNS.get(sport)
    if not patterns:
        return {}

    group_id = LEAGUE_IDS.get(sport)
    if not group_id:
        logger.warning(f"discover_prop_categories: no LEAGUE_IDS entry for {sport}")
        return {}

    url = f"{_NASH_BASE}/{group_id}"
    try:
        data = await _nash_get(url)
    except Exception as e:
        logger.warning(f"discover_prop_categories {sport}: {e}")
        return {}

    # Walk the Nash categorySet / offerCategories tree collecting (name, id) tuples.
    named_ids: list[tuple[str, int]] = []

    def _walk(node) -> None:
        if isinstance(node, dict):
            nm = node.get("name") or node.get("displayName") or ""
            cid = node.get("categoryId") or node.get("offerCategoryId") or node.get("subcategoryId")
            if nm and cid:
                try:
                    named_ids.append((str(nm).lower(), int(cid)))
                except (ValueError, TypeError):
                    pass
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)

    resolved: dict[str, int] = {}
    for prop_key, substr_list in patterns.items():
        for substr in substr_list:
            target = substr.lower()
            match = next((cid for nm, cid in named_ids if target in nm), None)
            if match is not None:
                resolved[prop_key] = match
                break

    _prop_category_cache[sport] = resolved
    logger.info(f"discover_prop_categories {sport}: resolved {len(resolved)}/{len(patterns)} markets")
    return resolved


def _effective_prop_categories(sport: str, resolved: Optional[dict[str, int]] = None) -> dict[str, int]:
    """Merge hard-coded DK_PROP_CATEGORIES with any runtime-resolved IDs.

    Runtime-resolved IDs win over the hard-coded ones — DK taxonomy drifts.
    """
    base = dict(DK_PROP_CATEGORIES.get(sport, {}))
    if resolved:
        base.update(resolved)
    # Drop zero-sentinel entries (unresolved, not scrape-able)
    return {k: v for k, v in base.items() if v}
