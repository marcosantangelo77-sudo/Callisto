"""Generic prediction resolver — any falsifiable claim, no sportsbook.

Backed by two domain-general core tables (tools/schema/core.py):

    predictions(id, claim_id, event_id, predicted_prob, book_implied_prob,
                odds_american, model_fair_prob, clv_prob_bp, context_key,
                created_at)
    outcomes(prediction_id PK REFERENCES predictions, resolved_outcome,
             payoff, resolved_at)

record_prediction()/record_outcome() are the writers; SqlitePredictionResolver
reads the pair back as EvidenceRecords. A Bitcoin hash-rate claim, a
materials-science forecast and an NBA spread all flow through this surface —
the lifecycle machinery above it never sees a domain noun.

InMemoryOutcomeResolver remains for in-process evaluation (tests, ingest
scripts) with no database at all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Iterable, Optional

from tools.resolvers.base import (
    OUTCOME_INDETERMINATE,
    OUTCOME_NEGATIVE,
    OUTCOME_POSITIVE,
    EvidenceRecord,
    OutcomeResolver,
    ResolutionSummary,
    _norm_outcome,
)

logger = logging.getLogger("callisto.resolvers.generic")

_DECIDED = {OUTCOME_POSITIVE, OUTCOME_NEGATIVE, OUTCOME_INDETERMINATE}


class InMemoryOutcomeResolver(OutcomeResolver):
    """Holds EvidenceRecords supplied by the caller (tests, ingest scripts)."""

    name = "generic"

    def __init__(self, records: Optional[Iterable[EvidenceRecord]] = None):
        self._records: list[EvidenceRecord] = list(records or [])

    def add(self, record: EvidenceRecord) -> None:
        self._records.append(record)

    async def iter_evidence(self, hypothesis_id: str) -> AsyncIterator[EvidenceRecord]:
        for r in self._records:
            yield r

    async def summarize(self, hypothesis_id: str) -> ResolutionSummary:
        return ResolutionSummary.from_records(self._records)


async def record_prediction(
    db,
    *,
    claim_id: str,
    event_id: str,
    predicted_prob: Optional[float] = None,
    book_implied_prob: Optional[float] = None,
    odds_american: Optional[int] = None,
    model_fair_prob: Optional[float] = None,
    clv_prob_bp: Optional[float] = None,
    context_key: Optional[str] = None,
    created_at: Optional[str] = None,
) -> int:
    """Commit one falsifiable instance of a recurring claim.

    Idempotent per (claim_id, event_id): if the same event is recorded twice
    (a retried pipeline step, a replayed backfill), the FIRST committed
    probability stands. Re-recording with a friendlier number after seeing
    evidence is exactly the laundering shape the lifecycle exists to prevent,
    so later writes cannot displace the original.

    Returns the prediction row id.
    """
    cur = await db.execute(
        "INSERT OR IGNORE INTO predictions "
        "(claim_id, event_id, predicted_prob, book_implied_prob, "
        " odds_american, model_fair_prob, clv_prob_bp, context_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            claim_id, event_id, predicted_prob, book_implied_prob,
            odds_american, model_fair_prob, clv_prob_bp, context_key,
            created_at or datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db.commit()
    lookup = await db.execute(
        "SELECT id FROM predictions WHERE claim_id = ? AND event_id = ?",
        (claim_id, event_id),
    )
    row = await lookup.fetchone()
    if row is None:
        raise RuntimeError(
            f"record_prediction: insert succeeded but row for "
            f"({claim_id!r}, {event_id!r}) is unreadable"
        )
    if cur.rowcount == 0:
        logger.info(
            "prediction for (%s, %s) already committed — first probability stands",
            claim_id, event_id,
        )
    return int(row[0])


async def record_outcome(
    db,
    *,
    prediction_id: int,
    resolved_outcome: str,
    payoff: Optional[float] = None,
    resolved_at: Optional[str] = None,
) -> None:
    """Attach ground truth to a prediction.

    ``resolved_outcome`` accepts any domain's token ('won', 'lost', 'push',
    'yes', 'no', 'confirmed', ...) normalised via the shared vocabulary.
    Overwrites are allowed and logged: an official correction (a league
    overturning a result) replaces stale truth. Unknown tokens raise rather
    than silently storing an unscorable row.
    """
    outcome = _norm_outcome(resolved_outcome)
    if outcome not in _DECIDED:
        raise ValueError(
            f"record_outcome: {resolved_outcome!r} does not resolve to a "
            f"scoreable outcome (positive/negative/indeterminate)"
        )
    await db.execute(
        "INSERT OR REPLACE INTO outcomes "
        "(prediction_id, resolved_outcome, payoff, resolved_at) "
        "VALUES (?, ?, ?, ?)",
        (
            prediction_id, outcome, payoff,
            resolved_at or datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db.commit()


class SqlitePredictionResolver(OutcomeResolver):
    """Reads the core predictions/outcomes tables as EvidenceRecords.

    Deliberately tolerant of pre-seam databases where the tables do not
    exist yet: it reports zero evidence rather than raising, so the
    lifecycle treats the claim as simply not-yet-tested.
    """

    name = "generic_sqlite"

    def __init__(self, db):
        self._db = db

    async def iter_evidence(self, hypothesis_id: str) -> AsyncIterator[EvidenceRecord]:
        try:
            cur = await self._db.execute(
                "SELECT p.event_id, p.predicted_prob, p.book_implied_prob, "
                "       p.odds_american, p.model_fair_prob, p.clv_prob_bp, "
                "       p.context_key, p.created_at, "
                "       o.resolved_outcome, o.payoff, o.resolved_at "
                "FROM predictions p LEFT JOIN outcomes o "
                "  ON o.prediction_id = p.id "
                "WHERE p.claim_id = ?",
                (hypothesis_id,),
            )
        except Exception:
            return
        cols = [d[0] for d in cur.description]
        for row in await cur.fetchall():
            d = dict(zip(cols, row))
            raw = (d.get("resolved_outcome") or "").strip().lower()
            yield EvidenceRecord(
                event_id=str(d.get("event_id") or ""),
                predicted_prob=d.get("predicted_prob"),
                resolved_outcome=_norm_outcome(raw) if raw else "unresolved",
                resolved_at=d.get("resolved_at") or d.get("created_at"),
                payoff=d.get("payoff"),
                odds_american=d.get("odds_american"),
                model_fair_prob=d.get("model_fair_prob"),
                book_implied_prob=d.get("book_implied_prob"),
                clv_prob_bp=d.get("clv_prob_bp"),
                context_key=d.get("context_key"),
                source=self.name,
            )


class GenericPredictionResolver:
    """Domain-general resolver facade.

    Choose a backend: ``GenericPredictionResolver.InMemory(records)`` for
    in-process evidence, or ``GenericPredictionResolver.Sqlite(db)`` to read
    the domain-general predictions/outcomes tables.
    """

    InMemory = InMemoryOutcomeResolver
    Sqlite = SqlitePredictionResolver