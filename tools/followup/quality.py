"""tools.followup.quality — the followup query quality gate.

Rejects vague / verbatim / entity-free followups so hallucinated
"next steps" never reach ``queue.submit_task``.
"""

from __future__ import annotations

import re


# Vague phrases that commonly appear in low-value LLM "next step" output.
# A followup whose query (after stripping the AUTO-FOLLOWUP header) is
# dominated by these gets rejected.
_VAGUE_PHRASES = (
    "investigate further",
    "look into this",
    "look into it",
    "dig deeper",
    "explore this",
    "more research needed",
    "further analysis",
    "to be determined",
    "tbd",
    "follow up",
    "follow-up",
    "keep monitoring",
    "keep watching",
    "revisit later",
    "needs more data",
)

# Patterns that indicate a concrete entity/reference in the query.
# The quality gate requires at least ONE of these. Matches are case-
# insensitive. A hit = "this followup is about a real thing".
_ENTITY_PATTERNS = (
    # Team/game/event IDs (numeric or alphanumeric)
    re.compile(r"\bevent[_\s-]?id[:\s=]*([a-z0-9_-]{3,})", re.I),
    re.compile(r"\bgame[_\s-]?id[:\s=]*([a-z0-9_-]{3,})", re.I),
    re.compile(r"\bhypothesis[_\s-]?id[:\s=]*([a-z0-9_-]{3,})", re.I),
    re.compile(r"\bplayer[_\s-]?id[:\s=]*([a-z0-9_-]{3,})", re.I),
    re.compile(r"\bsession[_\s-]?id[:\s=]*([a-z0-9-]{8,})", re.I),
    # Capitalised proper nouns (two+ Title-Case words = likely a player
    # or team name, e.g. "Jayson Tatum", "Atlanta Braves"). Weak signal
    # but catches most real entities.
    re.compile(r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}"),
    # Known hypothesis naming convention — ``mlb_early_home_fav`` etc.
    re.compile(r"\b[a-z]+_[a-z]+_[a-z]+(?:_[a-z]+)*\b"),
    # Dollar amounts, ML prices with sign, over/under lines
    re.compile(r"\b[+-]\d{2,4}\b"),
    re.compile(r"\b(?:over|under)\s+\d+(?:\.\d+)?", re.I),
)


def strip_followup_header(query: str) -> str:
    """Remove the leading ``AUTO-FOLLOWUP from task N:`` wrapper for comparison."""
    m = re.match(
        r"^\s*AUTO-FOLLOWUP\s+from\s+task\s+\d+\s*:\s*", query, flags=re.I
    )
    return query[m.end():].strip() if m else query.strip()


def token_edit_distance_ratio(a: str, b: str) -> float:
    """Ratio of differing tokens to total. 0 = identical, 1 = fully disjoint.

    Cheap proxy for Levenshtein — we care about "are these meaningfully
    different queries" not about exact distance. Tokenisation is whitespace
    + punctuation splitting; casing is normalised.
    """
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta and not tb:
        return 0.0
    union = ta | tb
    common = ta & tb
    if not union:
        return 0.0
    return 1.0 - (len(common) / len(union))


# Backwards-compatible private aliases used by earlier revisions of the
# monolithic module and by tests that poke at internals.
_strip_followup_header = strip_followup_header
_token_edit_distance_ratio = token_edit_distance_ratio


def evaluate_quality(parent_query: str, followup_query: str) -> tuple[bool, str]:
    """Return (passes_gate, reason).

    Rejects a followup query when ANY of:
      - shorter than 20 non-header characters (already length-gated upstream
        but we double-check here so direct test callers see consistent logic)
      - dominated by vague phrases with no concrete entity
      - identical (post-normalisation) to the parent
      - <30% token-level difference from the parent
      - contains no extractable entity pattern

    The gate is intentionally conservative: false-negatives (useful
    followups rejected) are cheap — the user can re-submit. False-positives
    (garbage followups accepted) cost credits and pollute the queue.
    """
    payload = _strip_followup_header(followup_query)
    parent_payload = _strip_followup_header(parent_query)

    if len(payload) < 20:
        return False, "query_too_short"

    # Verbatim or near-verbatim to parent.
    if payload.lower().strip() == parent_payload.lower().strip():
        return False, "verbatim_duplicate_of_parent"

    diff_ratio = _token_edit_distance_ratio(parent_payload, payload)
    if diff_ratio < 0.30:
        return False, f"too_similar_to_parent(diff_ratio={diff_ratio:.2f})"

    # Vague-phrase check. If the query IS one of the vague phrases (or
    # contains nothing beyond vague-phrase tokens), reject.
    low = payload.lower()
    vague_hit = any(phrase in low for phrase in _VAGUE_PHRASES)
    entity_hit = any(p.search(payload) for p in _ENTITY_PATTERNS)

    if vague_hit and not entity_hit:
        return False, "vague_language_no_entity"

    if not entity_hit:
        # Even without explicit vague phrases, no entity means we have
        # nothing concrete to research against.
        return False, "no_extractable_entity"

    return True, "ok"
