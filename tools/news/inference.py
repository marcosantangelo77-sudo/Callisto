"""Severity, status, and body-part inference from free-text news."""
from __future__ import annotations

import re
from typing import Optional

# Keyword -> (status, severity). Ordered: first match wins, so put
# strongest/most specific tokens first.
_SEVERITY_RULES: list[tuple[str, tuple[str, str]]] = [
    ("out for the season", ("out", "out_indefinite")),
    ("season-ending",      ("out", "out_indefinite")),
    ("out indefinitely",   ("out", "out_indefinite")),
    ("placed on ir",       ("out", "out_indefinite")),
    ("injured reserve",    ("out", "out_indefinite")),
    ("ruled out",          ("out", "severe")),
    ("will not play",      ("out", "severe")),
    ("inactive",           ("inactive", "severe")),
    ("doubtful",           ("doubtful", "moderate")),
    ("questionable",       ("questionable", "minor")),
    ("probable",           ("probable", "minor")),
    ("day-to-day",         ("questionable", "minor")),
    ("day to day",         ("questionable", "minor")),
    ("game-time decision", ("questionable", "minor")),
    ("game time decision", ("questionable", "minor")),
]

# ESPN's "status" strings often match our buckets directly. Map them and
# fall through to free-text inference if nothing lands.
_ESPN_STATUS_MAP = {
    "out":          ("out", "severe"),
    "doubtful":     ("doubtful", "moderate"),
    "questionable": ("questionable", "minor"),
    "probable":     ("probable", "minor"),
    "day-to-day":   ("questionable", "minor"),
    "active":       (None, None),
    "suspension":   ("inactive", "severe"),
    "suspended":    ("inactive", "severe"),
}


def infer_severity(
    status_text: Optional[str],
    detail_text: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(status, severity)`` from free-text + structured status.

    Inference order:
      1. Free-text keyword hit from _SEVERITY_RULES (most specific —
         "out for the season" is stronger than ESPN's generic "Out").
      2. Structured ESPN status → map directly.
      3. Fall back to (None, 'minor') — better a floor than no data.
    """
    combined = " ".join(filter(None, [status_text or "", detail_text or ""])).lower()

    # Run the keyword rules first; they're ordered strongest-first so the
    # first hit is the right answer. Critically this lets "out for the
    # season" upgrade a bare ESPN status='Out' to out_indefinite.
    for needle, (status, sev) in _SEVERITY_RULES:
        if needle in combined:
            return status, sev

    st_norm = (status_text or "").strip().lower()
    if st_norm in _ESPN_STATUS_MAP:
        mapped = _ESPN_STATUS_MAP[st_norm]
        if mapped != (None, None):
            return mapped

    # Nothing matched. If we had ANY status string, assume minor; else None.
    if st_norm:
        return st_norm or None, "minor"
    return None, None


# Body-part extraction is a shallow bag-of-tokens heuristic. Good enough for
# v1; upgrade to a dedicated NER model later if false-positive rate hurts.
_BODY_PART_MAP = {
    "lower_body": [
        "knee", "ankle", "foot", "heel", "toe", "hamstring", "quad",
        "calf", "groin", "hip", "leg", "shin", "achilles", "tibia",
    ],
    "upper_body": [
        "shoulder", "elbow", "wrist", "hand", "finger", "arm",
        "forearm", "bicep", "tricep", "pec", "chest", "collarbone",
    ],
    "core":      ["back", "oblique", "abdomen", "abdominal", "rib", "hip-flexor"],
    "head":      ["head", "concussion", "face", "jaw", "neck", "eye"],
    "illness":   ["illness", "flu", "covid", "sick", "virus"],
}


def infer_body_part(detail_text: Optional[str]) -> Optional[str]:
    if not detail_text:
        return None
    low = detail_text.lower()
    for bucket, tokens in _BODY_PART_MAP.items():
        for tok in tokens:
            # Word-boundary to avoid "back" matching "background".
            if re.search(rf"\b{re.escape(tok)}\b", low):
                return bucket
    return None
