"""
Centralized AGP thresholds.

Single source of truth for confidence tier boundaries, source-class ceilings,
escalation triggers, and the DB floor. Imported by agp, orchestrator, and memory.

Values mirror what was previously scattered across:
  - agp/__init__.py:39-48   (ConfidenceTier.from_score boundaries)
  - orchestrator.py:93-99   (MAX_CONFIDENCE_BY_SOURCE)
  - orchestrator.py:101     (ESCALATION_THRESHOLD)
  - memory.py:34            (DB CHECK constraint floor)

Do not change values here without also updating the DB schema CHECK constraint
and regression-testing downstream promotion gates.
"""

# ── Tier boundaries ──
# Used by ConfidenceTier.from_score to map a numeric score → categorical tier.
TIER_VERIFIED_MIN = 0.90
TIER_CORROBORATED_MIN = 0.75
TIER_PROBABLE_MIN = 0.55
TIER_SPECULATIVE_MIN = 0.30

# ── Source-class confidence ceilings ──
# A model cannot self-report higher confidence than its best evidence warrants.
# Keys are the .value of SourceClass enum members.
MAX_CONFIDENCE_BY_SOURCE: dict[str, float] = {
    "PRIMARY": 1.01,     # VERIFIED — direct analysis of primary documents
    "SECONDARY": 0.75,  # CORROBORATED — web search, third-party reports
    "SIGNAL": 0.55,     # PROBABLE — signals without primary corroboration
    "INFERRED": 0.55,   # PROBABLE — training data, no real-time verification
}

# Used when no tool calls happened during the session at all.
MAX_CONFIDENCE_NO_TOOL = 0.55

# ── Escalation ──
# Sessions ending below this score trigger a Claude Code enhancement pass
# (when available). Above it, Claude is still called but as a "review" pass.
ESCALATION_THRESHOLD = 0.60

# ── DB floor ──
# Matches the CHECK(confidence_score >= 0.30) constraint in memory.py schema.
# Sessions clamped below this cannot be stored at all.
DB_CONFIDENCE_FLOOR = 0.30

# ── Contradiction penalties ──
# Applied in orchestrator._step_manager_review, AFTER _clamp_confidence and
# BEFORE seal(). Floor is DB_CONFIDENCE_FLOOR; anything below that refuses to seal.
CONTRADICTION_PENALTY = {
    "CRITICAL": 0.15,
    "MAJOR": 0.05,
    "MINOR": 0.0,
}


def floor_conf(x: float, places: int = 2) -> float:
    """Quantise a confidence DOWNWARD. Never round.

    round(0.269183, 2) == 0.27 — an increase. That is small, and it is still an
    automated actor raising a confidence score, which is the one thing this
    architecture exists to make impossible. It also COMPOUNDS: a score passing
    through several clamp/penalty round-trips can creep upward with no evidence
    behind it. A red-team pass found it in apply_verdict, in the provenance
    clamp, and in the panel path after an earlier fix landed in only one of
    them — which is why this lives in ONE place that every caller uses.
    """
    import math
    f = 10 ** places
    return math.floor(float(x) * f) / f
