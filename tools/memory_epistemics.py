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
    verifies, or they fall back to INFERRED. The wiki's >= 0.5 compile gate
    therefore cannot be climbed by ACCUMULATION: with the ratchet dead there
    is no path from repeated guesses to a rising stored value.
    (Honesty note, improve/memory-wiki: an earlier revision of this paragraph
    claimed the gate "cannot be reached by an unverified guess alone". That
    was numerically false — the INFERRED ceiling is 0.55, ABOVE the 0.5 gate.
    What actually bounds that path now is decay: the wiki admits on DECAYED
    effective confidence, so a single unverified 0.55 guess stays
    wiki-admissible only ~1.9 days without re-observation.)

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


def verify_seal_method(session: dict, seal_hash: str | None) -> str | None:
    """Which verification path *seal_hash* passes over *session*, or None.

    Returns one of:
      "unsealed"       — no seal_hash at all (caller must treat as INFERRED)
      "keyed"          — HMAC under the current key
      "rotation"       — HMAC under a rotation key (CALLISTO_SEAL_KEY_OLD)
      "unkeyed-regime" — digest matched the plain SHA-256 while NO key is
                         configured (the pre-keying regime itself)
      "legacy-fallback"— digest matched ONLY the public SHA-256 while a key
                         IS configured. This proves nothing: anyone can
                         recompute it (red-team R5). Callers gating a claimed
                         provenance class MUST treat this as failure.
      None             — no candidate matched / malformed input
    """
    if not session:
        return None
    if not seal_hash:
        return "unsealed"
    payload = _canonical_payload(session)
    provided = str(seal_hash)
    keys = _seal_keys()
    if keys:
        for i, key in enumerate(keys):
            cand = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
            if hmac.compare_digest(cand, provided):
                return "keyed" if i == 0 else "rotation"
        # Keyed regime: the public hash is forgeable by anyone — never proof.
        legacy = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return "legacy-fallback" if hmac.compare_digest(legacy, provided) else None
    # No key configured: the operating regime IS unkeyed; matching the plain
    # SHA-256 proves the bytes are intact but proves nothing about WHO sealed
    # them — anyone with DB access can recompute it (red-team R5). Under an
    # unkeyed regime a seal therefore verifies integrity only; admit_learning
    # treats it as insufficient to honor a claimed class above INFERRED.
    legacy = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return "unkeyed-regime" if hmac.compare_digest(legacy, provided) else None


def verify_learning_seal(session: dict, seal_hash: str | None) -> bool:
    """True iff *seal_hash* is a valid seal over *session* under the current
    or rotation key. Under an unkeyed regime (no CALLISTO_SEAL_KEY) this
    returns False for everything except the trivially-consistent no-seal
    case: an unkeyed digest is forgeable by anyone, so it can never back a
    provenance claim (see verify_seal_method; red-team R5).

    NOTE: under a keyed regime this deliberately returns False for a digest
    that matches only the legacy public SHA-256 — see verify_seal_method.
    """
    return verify_seal_method(session, seal_hash) in (
        "keyed", "rotation",
    )


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
        else:
            method = verify_seal_method(seal_session, seal_hash)
            if method in ("legacy-fallback", "unkeyed-regime"):
                # The digest is the public SHA-256 — forgeable by anyone
                # with DB access (red-team R5), whether or not a key is
                # configured. Fail closed in both regimes.
                cls = "INFERRED"
                reason = (f"{method} digest → forgeable, collapsed to "
                          "INFERRED (fail closed)")
            elif not method or method == "unsealed":
                cls = "INFERRED"
                reason = "seal failed verification → collapsed to INFERRED (fail closed)"
            else:
                reason = f"seal verified ({method}) → claimed class honored"
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

# (stance ranking is inlined in trim_learnings_for_context; kept for callers
# that want the canonical ordering)
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

    Policy (in order):
      1. EVERY contradicting item survives, however many there are (matching
         compact_state's never-drop-contradicting rule). The budget applies
         only to supporting/neutral items.
      2. Remaining budget goes to supporting/neutral items, ranked by
         stance (neutral before supporting), then better tier, then higher
         effective confidence, then id for determinism.

    Unknown/missing stance → 'supporting' for MEMORY items (memory entries
    are assertions of pattern; only explicitly-marked disconfirmations count
    as contradicting — the conservative direction, since misclassifying a
    supporting item as contradicting would give it unearned protection).

    Returns (kept, dropped); dropped items gain ``dropped_reason``.
    """
    def rank_key(it: dict):
        stance = str(it.get("stance") or "supporting").lower()
        stance_rank = 0 if stance == "neutral" else 1
        try:
            tier = int(it.get("tier", 3))
        except (TypeError, ValueError):
            tier = 3
        try:
            eff = -float(it.get("effective_confidence", 0.0))
        except (TypeError, ValueError):
            eff = 0.0
        return (stance_rank, tier, eff, str(it.get("id")))

    contradicting = [dict(it) for it in items
                     if str(it.get("stance") or "supporting").lower() == "contradicting"]
    rest = [dict(it) for it in items
            if str(it.get("stance") or "supporting").lower() != "contradicting"]

    kept = list(contradicting)
    dropped = []
    remaining = max(0, max_items - len(kept))
    ordered = sorted(rest, key=rank_key)
    kept.extend(ordered[:remaining])
    for it in ordered[remaining:]:
        it["dropped_reason"] = (
            f"context budget ({max_items}) — supporting/neutral trimmed before "
            f"any contradicting item; lowest priority dropped first"
        )
        dropped.append(it)
    return kept, dropped


# ── Provenance annotation for reinjection ───────────────────────────────────

def annotate_for_reinjection(row: dict) -> dict:
    """Attach provenance + ceiling to a learning row destined for a prompt.

    The emitted dict always carries source_class and confidence_ceiling so
    downstream prompt-builders (and the wiki's source admission) cannot treat
    a reinjected INFERRED learning as PRIMARY evidence.

    Effective-confidence resolution, in order:
      1. a caller-provided ``effective_confidence`` (e.g. already passed
         through decay_confidence) is honoured and capped by the ceiling;
      2. otherwise the raw stored ``confidence`` is used, capped likewise.

    History (improve/memory-wiki): this function previously OVERWROTE any
    caller-provided effective_confidence with min(raw, ceiling), which made
    read-time decay dead code on every prompt path — a 100-day-old learning
    emitted at full stored confidence. Callers that compute decay must see it
    survive; callers that do not get the old capped-raw behaviour unchanged.
    """
    cls = normalize_source_class(row.get("source_class")) or "INFERRED"
    ceiling = PROVENANCE_CEILINGS[cls]
    out = dict(row)
    out["source_class"] = cls
    out["confidence_ceiling"] = ceiling
    provided = row.get("effective_confidence")
    if provided is not None:
        eff = float(provided)
    else:
        eff = float(row.get("confidence", 0.0))
    # Cap by class ceiling; never introduce upward rounding here — the only
    # arithmetic is min(), so the value can only fall.
    out["effective_confidence"] = max(0.0, min(eff, ceiling))
    return out
