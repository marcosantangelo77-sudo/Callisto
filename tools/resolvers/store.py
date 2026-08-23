"""PredictionStore — the write side of the domain-general evidence path.

The read side (``SqlitePredictionResolver``) has existed since B1, but
until migration 016 the tables it reads were created only inside a test,
and nothing in production code could record a prediction or resolve an
outcome. This module is the minimal writer:

    store.record(claim_id, event_id, predicted_prob=..., context_key=...)
    store.resolve(prediction_id, "confirmed", payoff=4.0)
    # then SqlitePredictionResolver(db).summarize(claim_id) scores it

Design points:
* Predictions are append-only. A recorded prediction is never edited or
  deleted — preregistration semantics at storage level. ``resolve`` is the
  only mutation, it targets one row by id, and re-resolution is rejected
  unless ``overwrite=True`` (corrections carry an explicit flag rather
  than silently rewriting history).
* Outcome tokens pass through ``_norm_outcome`` so any domain's vocabulary
  ("confirmed", "retracted", "won", "yes") lands on the general one.
* No domain nouns anywhere; sports keeps its own paper_trades/clv_log
  path untouched.

Nothing here arms execution or touches money.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("callisto.prediction_store")

# Re-resolution policy: reject a second verdict on an already-resolved
# prediction unless the caller explicitly flags a correction.
_CORRECTION_FLAG = "--correction"


class PredictionStore:
    """Writes predictions/outcomes rows for any falsifiable claim."""

    def __init__(self, db):  # aiosqlite connection
        self._db = db

    async def record(
        self,
        claim_id: str,
        event_id: str,
        *,
        predicted_prob: Optional[float] = None,
        context_key: Optional[str] = None,
    ) -> int:
        """Record a prediction BEFORE ground truth arrives. Returns its id."""
        if not claim_id or not event_id:
            raise ValueError("claim_id and event_id are required")
        cur = await self._db.execute(
            "INSERT INTO predictions (claim_id, event_id, predicted_prob, "
            "context_key) VALUES (?, ?, ?, ?)",
            (claim_id, event_id, predicted_prob, context_key),
        )
        await self._db.commit()
        return int(cur.lastrowid)

    async def resolve(
        self,
        prediction_id: int,
        outcome: str,
        *,
        payoff: Optional[float] = None,
        overwrite: bool = False,
    ) -> bool:
        """Resolve a prediction against ground truth. Returns True when the
        row changed. Unknown outcome tokens raise — a typo'd verdict must
        not silently become 'unresolved' and vanish from scoring."""
        from tools.resolvers.base import _norm_outcome

        normed = _norm_outcome(outcome)
        if normed == "unresolved":
            raise ValueError(f"unrecognised outcome token {outcome!r}")
        cur = await self._db.execute(
            "SELECT resolved_outcome FROM outcomes WHERE prediction_id = ?",
            (prediction_id,),
        )
        row = await cur.fetchone()
        if row is not None:
            if not overwrite:
                logger.warning(
                    "prediction %d already resolved as %r; pass "
                    "overwrite=True to correct", prediction_id, row[0])
                return False
            await self._db.execute(
                "UPDATE outcomes SET resolved_outcome = ?, payoff = ?, "
                "resolved_at = CURRENT_TIMESTAMP WHERE prediction_id = ?",
                (normed, payoff, prediction_id),
            )
        else:
            await self._db.execute(
                "INSERT INTO outcomes (prediction_id, resolved_outcome, "
                "payoff) VALUES (?, ?, ?)",
                (prediction_id, normed, payoff),
            )
        await self._db.commit()
        return True

    async def get(self, prediction_id: int) -> Optional[dict]:
        cur = await self._db.execute(
            "SELECT p.id, p.claim_id, p.event_id, p.predicted_prob, "
            "p.context_key, p.created_at, o.resolved_outcome, o.payoff, "
            "o.resolved_at FROM predictions p "
            "LEFT JOIN outcomes o ON o.prediction_id = p.id "
            "WHERE p.id = ?",
            (prediction_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
