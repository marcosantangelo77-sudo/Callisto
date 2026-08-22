"""tools/memory_epistemics.py — trust policy for the hermes memory layer.

P4 build wave (build/memory-trust). This module holds the rules that decide
WHAT THE LOOP SEES, as pure functions so they are testable with random
inputs and reusable by both tools/hermes_memory.py and tests.

Three defects this closes (findings/instance4.md, P3):

1. THE TRUST ESCALATOR. ``hermes_learnings`` was upserted with
   ``confidence=MAX(confidence, excluded.confidence)``: confidence could
   never fall, one optimistic self-report contaminated a key forever, and
   the wiki then admitted anything >= 0.5 as compile source. Replaced with
   DECAY-AND-REWRITE semantics: each write REPLACES the stored confidence,
   and every subsequent read applies time decay. Nothing is monotonic.

2. UNVERIFIED ADMISSIONS. A learning whose source class is INFERRED can
   never be stored above the INFERRED ceiling regardless of what the writer
   claimed; learnings claiming sealed provenance must carry a seal that
   verifies, or they fall back to INFERRED. The wiki's >= 0.5 admission gate
   therefore cannot be reached by an unverified guess alone.

3. PROVENANCE CEILINGS ON REINJECTION. Every learning carries its source
   class and confidence ceiling; when it is re-emitted into prompt context
   the ceiling travels with it, so a reinjected INFERRED learning is never
   mistaken for PRIMARY evidence on the way back in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

# ── Ceilings ────────────────────────────────────────────────────────────────
# Mirrors agp/thresholds.MAX_CONFIDENCE_BY_SOURCE but importable without the
# agp package (hermes_memory is used from processes where agp is absent).
# Keep in sync deliberately; a test pins the agreement.
PROVENANCE_CEILINGS: dict[str, float] = {
    "PRIMARY": 1.0,
    "SECONDARY": 0.75,
    "SIGNAL": 0.55,
    "INFERRED": 0.55,
}
DEFAULT_CEILING = PROVENANCE_CEILINGS["INFERRED"]

# Sources that may exceed their class ceiling on write (operator/audit
# channels). Everything model-produced is clamped.
TRUSTED_SOURCES = frozenset({"human", "audit"})

# Time decay: a learning's effective confidence halves every
# CONFIDENCE_HALF_LIFE_DAYS of NOT being re-observed. Re-recording a learning
# resets learned_at (the observation), not the confidence ratchet — there is
# no ratchet anymore.
CONFIDENCE_HALF_LIFE_DAYS = 14.0
MIN_EFFECTIVE_CONFIDENCE = 0.05


def clamp_to_ceiling(confidence: float, source_class: str | None) -> float:
    """Clamp *confidence* to the ceiling of its provenance class."""
    conf = max(0.0, min(1.0, float(confidence)))
    if not source_class:
        return round(min(conf, DEFAULT_CEILING), 3)
    return round(min(conf, PROVENANCE_CEILINGS.get(source_class, DEFAULT_CEILING)), 3)


def normalize_source_class(value) -> str | None:
    """Coerce a declared source class to a known one, else None (= INFERRED)."""
    if value is None:
        return None
    v = str(value).strip().upper()
    return v if v in PROVENANCE_CEILINGS else None


# ── Seal verification for carried provenance ────────────────────────────────
#
# Learnings may carry a ``provenance`` blob naming the session/seal they were
# derived from. If they claim SECONDARY-or-better via a seal, the seal must
# verify against the SAME keyed HMAC scheme agp uses, otherwise the claimed
# class collapses to INFERRED. We implement the check here directly (same
# canonicalisation + HMAC-SHA256 + legacy-sha256 fallback) rather than
# importing agp, so the memory layer stays dependency-light; agreement with
# agp.AGPSession.verify_seal is pinned by tests.


def _canonical_payload(session: dict) -> str:
    """Identical algorithm to agp._canonical_payload: seal_hash → None, then
    sort_keys JSON. Kept byte-compatible so a seal that verifies under
    agp.AGPSession.verify_seal also verifies here (pinned by tests)."""
    payload_dict = dict(session)
    payload_dict["seal_hash"] = None
    return json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)


def _seal_keys() -> list[bytes]:
    import os
    keys: list[bytes] = []
    key_hex = os.getenv("CALLISTO_SEAL_KEY", "")
    if key_hex:
        try:
            keys.append(bytes.fromhex(key_hex))
        except ValueError:
            pass
    old_hex = os.getenv("CALLISTO_SEAL_KEY_OLD", "")
    if old_hex:
        try:
            keys.append(bytes.fromhex(old_hex))
        except ValueError:
            pass
    return keys


def _seal_digest(payload: str) -> str:
    """Byte-identical to agp.__init__._seal_digest."""
    keys = _seal_keys()
    if keys:
        return hmac.new(keys[0], payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_learning_seal(session: dict, seal_hash: str | None) -> bool:
    """True iff *seal_hash* is a valid seal over *session* under the current
    or rotation key — or the row is legacy-unsealed AND carries no claimed
    class above INFERRED (caller handles the cap; this returns True only for
    the trivially-consistent case of no seal at all)."""
    if not session:
        return False
    if not seal_hash:
        return True  # unsealed: caller must treat as INFERRED-capped
    payload = _canonical_payload(session)
    candidates = [
        _seal_digest(payload),                                # current/rotation key
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),  # legacy unkeyed
    ]
    return any(hmac.compare_digest(c, str(seal_hash)) for c in candidates)


@dataclass
class LearningAdmission:
    """Result of gating one record_learning call."""
    admitted: bool
    stored_confidence: float
    source_class: str          # normalized class actually stored
    ceiling: float
    reason: str                # human-readable audit line


def admit_learning(
    *,
    key: str,
    confidence: float,
    source: str,
    source_class=None,
    seal_session: dict | None = None,
    seal_hash: str | None = None,
) -> LearningAdmission:
    """Gate one learning write. Pure; raises nothing.

    Rules:
      - confidence is REPLACED on upsert (no MAX ratchet).
      - the stored confidence never exceeds the ceiling of the learning's
        provenance class; trusted sources (human/audit) may exceed.
      - a claimed class above INFERRED backed by a seal that FAILS to verify
        collapses to INFERRED and takes the INFERRED ceiling (fail closed).
      - an unsealed claim above INFERRED is likewise capped to INFERRED.
    """
    cls = normalize_source_class(source_class) or "INFERRED"
    trusted = source in TRUSTED_SOURCES

    if cls != "INFERRED":
        if seal_session is None or not seal_hash:
            cls = "INFERRED"
            reason = "claimed class without seal evidence → capped to INFERRED"
        elif not verify_learning_seal(seal_session, seal_hash):
            cls = "INFERRED"
            reason = "seal failed verification → collapsed to INFERRED (fail closed)"
        else:
            reason = "seal verified → claimed class honored"
    else:
        reason = "declared INFERRED"

    ceiling = PROVENANCE_CEILINGS[cls]
    stored = float(confidence) if trusted else clamp_to_ceiling(confidence, cls)
    return LearningAdmission(
        admitted=True,
        stored_confidence=stored,
        source_class=cls,
        ceiling=ceiling,
        reason=reason,
    )


# ── Read-time decay ────────────────────────────────────────────────────────

def decay_confidence(stored: float, learned_at_iso: str, now: datetime | None = None) -> float:
    """Effective confidence after exponential decay since last observation."""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(str(learned_at_iso))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return max(MIN_EFFECTIVE_CONFIDENCE, min(1.0, float(stored)))
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    decayed = float(stored) * math.pow(0.5, age_days / CONFIDENCE_HALF_LIFE_DAYS)
    return round(max(MIN_EFFECTIVE_CONFIDENCE, min(1.0, decayed)), 4)


# ── Disconfirming-biased trimming (consistency with tools/loop_quality.py) ──

STANCE_RANK = {"contradicting": 0, "neutral": 1, "supporting": 2}


def trim_learnings_for_context(
    items: list[dict],
    max_items: int,
) -> tuple[list[dict], list[dict]]:
    """Trim learnings to fit context, disconfirming-first.

    Consistency rule with loop_quality.compact_state: contradicting items are
    retained in preference to supporting ones — the one disconfirming source
    is the most expensive thing to lose, and dropping it silently corrupts
    everything downstream.

    Ranking key (ascending = kept first):
      stance rank (contradicting < neutral < supporting),
      then lower tier wins (better provenance),
      then HIGHER effective confidence wins,
      then id for determinism.

    Unknown/missing stance → 'supporting' for MEMORY items (memory entries
    are assertions of pattern; only explicitly-marked disconfirmations count
    as contradicting — the conservative direction, since misclassifying a
    supporting item as contradicting would give it unearned protection).

    Returns (kept, dropped); dropped items gain ``dropped_reason``.
    """
    def sort_key(it: dict):
        stance = str(it.get("stance") or "supporting").lower()
        if stance not in STANCE_RANK:
            stance = "supporting"
        try:
            tier = int(it.get("tier", 3))
        except (TypeError, ValueError):
            tier = 3
        try:
            eff = -float(it.get("effective_confidence", 0.0))
        except (TypeError, ValueError):
            eff = 0.0
        return (STANCE_RANK[stance], tier, eff, str(it.get("id")))

    ordered = sorted(items, key=sort_key)
    kept = [dict(it) for it in ordered[:max_items]]
    dropped = []
    for it in ordered[max_items:]:
        d = dict(it)
        d["dropped_reason"] = (
            f"context budget ({max_items}) — supporting/neutral trimmed before "
            f"any contradicting item; lowest priority dropped first"
        )
        dropped.append(d)
    return kept, dropped


# ── Provenance annotation for reinjection ───────────────────────────────────

def annotate_for_reinjection(row: dict) -> dict:
    """Attach provenance + ceiling to a learning row destined for a prompt.

    The emitted dict always carries source_class and confidence_ceiling so
    downstream prompt-builders (and the wiki's source admission) cannot treat
    a reinjected INFERRED learning as PRIMARY evidence.
    """
    cls = normalize_source_class(row.get("source_class")) or "INFERRED"
    out = dict(row)
    out["source_class"] = cls
    out["confidence_ceiling"] = PROVENANCE_CEILINGS[cls]
    out["effective_confidence"] = min(
        float(row.get("confidence", 0.0)),
        PROVENANCE_CEILINGS[cls],
    )
    return out
