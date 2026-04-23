"""
player_name_index — Fuzzy match odds-API player names to player_stats canonical.

Why this exists
---------------
Odds APIs (DraftKings, Odds-API.io, Fanatics) and stats feeds (MLB StatsAPI,
ESPN, NHL API) disagree about player name surface form:

  * "Shohei Ohtani"  <-> "S. Ohtani"   <-> "Ohtani, Shohei" <-> "Ohtani"
  * "Connor McDavid" <-> "C. McDavid"
  * "T.J. Watt"      <-> "TJ Watt"     <-> "Watt, T.J."

Each outbound system has its own normalisation. The single source of truth
in Callisto is whatever MLB-StatsAPI / NHL-API / ESPN wrote into
``player_stats.player_name``. That's the canonical form.

The index builds a table ``player_names`` (canonical, alias, sport,
last_seen) seeded from ``player_stats``, and exposes a resolver that maps
arbitrary prop names to the canonical form, with a confidence score.

Matching strategy — in order, stop at first hit ≥ threshold:
  1. Exact match (case-insensitive) on alias
  2. Exact match (case-insensitive) on canonical
  3. Structural match — handle "S. Ohtani" / "Ohtani, Shohei" patterns
     by tokenising and aligning first-initial + last-name
  4. SequenceMatcher ratio on the normalised form

Confidence score is in ``[0, 1]``. The threshold enforced by callers is
0.90 — the spec requires this for prop resolution to avoid misattribution
(Player A's points credited to Player B).

The table is idempotent (UNIQUE (sport, canonical, alias_norm)) and
self-heals: every successful resolution re-INSERTs so aliases bubble up
without needing a dedicated migration.
"""
from __future__ import annotations

import difflib
import logging
import re
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.90

# Suffix tokens we ignore when normalising (so "Jr"/"Sr"/"III" don't
# wreck the ratio).
_SUFFIX_TOKENS = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}

# Characters we strip when normalising — punctuation and diacritics would
# make SequenceMatcher noisy.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _strip_suffix(tokens: list[str]) -> list[str]:
    """Drop Jr/Sr/III trailing tokens."""
    out = list(tokens)
    while out and out[-1].lower() in _SUFFIX_TOKENS:
        out.pop()
    return out


def _normalise(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, drop suffixes."""
    if not name:
        return ""
    # Handle "Ohtani, Shohei" → "Shohei Ohtani"
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            name = f"{parts[1]} {parts[0]}"
    s = _PUNCT_RE.sub(" ", name.lower())
    tokens = [t for t in s.split() if t]
    tokens = _strip_suffix(tokens)
    return " ".join(tokens)


def _first_initial_last(name: str) -> Optional[tuple[str, str]]:
    """Return (first_initial, last_token) for structural matching.

    ``"Shohei Ohtani"`` -> ``("s", "ohtani")``
    ``"S. Ohtani"``     -> ``("s", "ohtani")``
    ``"Ohtani"``        -> ``None`` (single token; can't structural-match)
    """
    norm = _normalise(name)
    tokens = [t for t in norm.split() if t]
    if len(tokens) < 2:
        return None
    return tokens[0][0], tokens[-1]


def fuzzy_match_score(query: str, candidate: str) -> float:
    """Return a confidence score in [0, 1] for (query, candidate) pair.

    Rules:
      * Exact (case/punct-insensitive) match -> 1.0
      * First-initial + last-name structural match -> 0.92
        (covers "S. Ohtani" <-> "Shohei Ohtani")
      * Last-name-only match (query has just "Ohtani") -> 0.85 if it's
        the only candidate with that surname, else 0.70 (ambiguous)
      * Otherwise, SequenceMatcher ratio on normalised forms.
    """
    q = _normalise(query)
    c = _normalise(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0

    # Structural match on first-initial + last-name
    q_struct = _first_initial_last(query)
    c_struct = _first_initial_last(candidate)
    if q_struct and c_struct and q_struct == c_struct:
        return 0.92

    q_tokens = q.split()
    c_tokens = c.split()
    # Single-token query (just surname) — rank against candidate's last token.
    if len(q_tokens) == 1 and c_tokens:
        if q_tokens[0] == c_tokens[-1]:
            return 0.70   # ambiguous by default; caller can upgrade
    # Fallback: raw sequence ratio
    return difflib.SequenceMatcher(None, q, c).ratio()


class PlayerNameIndex:
    """Async index over ``player_names`` with fuzzy resolution helpers."""

    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    async def ensure_schema(self) -> None:
        """Create the ``player_names`` table if absent. Safe to call often."""
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS player_names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                canonical TEXT NOT NULL,
                alias TEXT NOT NULL,
                alias_norm TEXT NOT NULL,
                source TEXT,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sport, canonical, alias_norm)
            )
            """
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_player_names_lookup "
            "ON player_names(sport, alias_norm)"
        )
        await self._db.commit()

    async def seed_from_player_stats(self, sport: Optional[str] = None,
                                     limit: int = 100000) -> int:
        """Bootstrap aliases from distinct ``player_stats.player_name``.

        Canonical == alias for the seed; fuzzy hits get added incrementally.
        Returns the number of rows upserted.
        """
        if sport:
            q = (
                "SELECT DISTINCT sport, player_name FROM player_stats "
                "WHERE sport = ? AND player_name IS NOT NULL LIMIT ?"
            )
            args: tuple = (sport, limit)
        else:
            q = (
                "SELECT DISTINCT sport, player_name FROM player_stats "
                "WHERE player_name IS NOT NULL LIMIT ?"
            )
            args = (limit,)
        cur = await self._db.execute(q, args)
        rows = await cur.fetchall()
        upserted = 0
        for sp, name in rows:
            nm = (name or "").strip()
            if not nm:
                continue
            norm = _normalise(nm)
            try:
                await self._db.execute(
                    "INSERT OR IGNORE INTO player_names "
                    "(sport, canonical, alias, alias_norm, source) "
                    "VALUES (?, ?, ?, ?, 'player_stats_seed')",
                    (sp, nm, nm, norm),
                )
                upserted += 1
            except Exception as e:
                logger.debug(f"player_names seed skip ({sp}/{nm}): {e}")
        await self._db.commit()
        return upserted

    async def resolve(
        self,
        sport: str,
        query_name: str,
        threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> Optional[tuple[str, float]]:
        """Resolve ``query_name`` to a canonical name for ``sport``.

        Returns ``(canonical, confidence)`` or ``None`` if no match is
        ≥ threshold.

        The cheap path (cached exact alias hit) is one indexed SELECT. The
        slow path scans distinct canonicals for the sport (usually a few
        thousand rows) and runs fuzzy_match_score on each.
        """
        if not query_name or not sport:
            return None
        norm = _normalise(query_name)
        if not norm:
            return None

        # Fast path — cached alias hit.
        cur = await self._db.execute(
            "SELECT canonical FROM player_names "
            "WHERE sport = ? AND alias_norm = ? LIMIT 1",
            (sport, norm),
        )
        row = await cur.fetchone()
        if row:
            return row[0], 1.0

        # Slow path — fuzzy against distinct canonicals for the sport.
        cur = await self._db.execute(
            "SELECT DISTINCT canonical FROM player_names WHERE sport = ?",
            (sport,),
        )
        candidates = [r[0] for r in await cur.fetchall()]
        if not candidates:
            # Fall back to raw player_stats in case the index wasn't seeded
            # (e.g. tests that skip seeding).
            cur = await self._db.execute(
                "SELECT DISTINCT player_name FROM player_stats "
                "WHERE sport = ? AND player_name IS NOT NULL",
                (sport,),
            )
            candidates = [r[0] for r in await cur.fetchall()]

        best_name: Optional[str] = None
        best_score = 0.0
        # For single-token queries, upgrade last-name-only match to high
        # confidence only if it's unambiguous (exactly one candidate with
        # that surname).
        q_is_single = len(norm.split()) == 1
        last_name_hits: list[str] = []
        if q_is_single:
            for c in candidates:
                c_tokens = _normalise(c).split()
                if c_tokens and c_tokens[-1] == norm:
                    last_name_hits.append(c)

        for c in candidates:
            score = fuzzy_match_score(query_name, c)
            if q_is_single and c in last_name_hits and len(last_name_hits) == 1:
                score = max(score, 0.91)
            if score > best_score:
                best_score = score
                best_name = c
                if best_score >= 0.999:
                    break

        if best_name is None or best_score < threshold:
            return None

        # Persist the alias so future queries hit the fast path.
        try:
            await self._db.execute(
                "INSERT OR IGNORE INTO player_names "
                "(sport, canonical, alias, alias_norm, source) "
                "VALUES (?, ?, ?, ?, 'fuzzy_hit')",
                (sport, best_name, query_name, norm),
            )
            await self._db.commit()
        except Exception as e:
            logger.debug(f"player_names alias insert skip: {e}")

        return best_name, best_score


__all__ = [
    "PlayerNameIndex",
    "fuzzy_match_score",
    "DEFAULT_CONFIDENCE_THRESHOLD",
]
