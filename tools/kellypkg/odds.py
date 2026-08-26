"""Odds / confidence-tier helpers (split from tools/kelly.py)."""


def _american_to_decimal(american):
    """Convert American odds to decimal odds."""
    if american > 0:
        return 1.0 + (american / 100.0)
    elif american < 0:
        return 1.0 + (100.0 / abs(american))
    else:
        return 2.0  # even money


def _confidence_tier_from_score(score):
    """Map a 0-1 confidence score to its AGP tier string."""
    if score >= 0.90:
        return "VERIFIED"
    elif score >= 0.75:
        return "CORROBORATED"
    elif score >= 0.55:
        return "PROBABLE"
    elif score >= 0.30:
        return "SPECULATIVE"
    else:
        return "UNVERIFIED"
