"""Generic prediction resolver — any falsifiable claim, no sportsbook.

Backed by two domain-general tables that the schema seam (BUILD_MANDATE
queue item 8) will formalise:

    predictions(id, claim_id, event_id, predicted_prob, context_key,
                created_at)
    outcomes(prediction_id, resolved_outcome, payoff, resolved_at)

Until those tables exist in a deployment, callers can use
InMemoryOutcomeResolver to hold evidence for evaluation — which is enough
for a Bitcoin hash-rate claim or a materials-science forecast to enter the
lifecycle scoring path today without touching the sports schema.
"""

from __future__ import annotations

from typing import AsyncIterator, Iterable, Optional

from tools.resolvers.base import (
    EvidenceRecord,
    OutcomeResolver,
    ResolutionSummary,
    _norm_outcome,
)


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


class SqlitePredictionResolver(OutcomeResolver):
    """Reads predictions/outcomes tables when present; empty otherwise.

    Deliberately tolerant: if the tables don't exist yet (schema seam not
    landed), it reports zero evidence rather than raising, so the lifecycle
    can treat the claim as simply not-yet-tested.
    """

    name = "generic_sqlite"

    def __init__(self, db):
        self._db = db

    async def iter_evidence(self, hypothesis_id: str) -> AsyncIterator[EvidenceRecord]:
        try:
            cur = await self._db.execute(
                "SELECT p.event_id, p.predicted_prob, p.context_key, p.created_at, "
                "       o.resolved_outcome, o.payoff, o.resolved_at "
                "FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.id "
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
