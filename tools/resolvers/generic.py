"""Generic prediction resolver — any falsifiable claim, no sportsbook.

Backed by two domain-general tables that the schema seam formalised
(tools/schema/core.py — ``predictions`` and ``outcomes``, applied by
ensure_schema on every boot):

    predictions(id, claim_id, event_id, predicted_prob, context_key,
                due_at, created_at)
    outcomes(prediction_id, resolved_outcome, payoff, resolved_at,
             resolved_by)

PredictionJournal is the WRITE side of that seam — how a question becomes
a recurring hypothesis (NEXT.md's core reframe) and how its predictions
resolve into earned confidence. The resolver classes below stay read-only
adapters over ground truth; the journal only records what a human or a
settled market declares, never what the model infers.

Fail-closed by construction:
  - a prediction without a probability in [0,1] cannot be inserted
    (validated here AND enforced by the table CHECK);
  - a prediction cannot reference a claim that does not exist;
  - an outcome token outside the recognised vocabulary is refused, not
    silently coerced to indeterminate;
  - a prediction resolves once (outcomes.prediction_id IS the primary key);
    re-resolving with a different outcome is refused, identical re-entry
    is an idempotent no-op;
  - a prediction past its due_at with no outcome scores STALE — unresolved
    by its own deadline — feeding the inheritance rule's staleness penalty.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
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


# ────────────────────────────────────────────────────────────────────────────
# The write side — questions become recurring hypotheses, predictions
# resolve, track records feed the inheritance rule.
# ────────────────────────────────────────────────────────────────────────────

class PredictionJournalError(Exception):
    """Refusal to record. Every raise here is fail-closed by design."""


# General claims live in the shared lifecycle table (hypotheses) under an
# honest general marker. Storage names do not change (STAGE_SEMANTICS);
# sport='general' + market_type='forecast' is a LABEL, and every consumer
# that keys on sport simply never matches it.
GENERAL_CLAIM_SPORT = "general"
GENERAL_CLAIM_MARKET = "forecast"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_when(raw: Optional[str]) -> Optional[datetime]:
    """Parse a deadline/due string: date-only or full ISO; None allowed."""
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        d = date.fromisoformat(t)
    except ValueError as exc:
        raise PredictionJournalError(
            f"unparseable date {raw!r} (want YYYY-MM-DD or ISO datetime)"
        ) from exc
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


class PredictionJournal:
    """Preregistered forward-testing for any falsifiable claim.

    Writes the tables SqlitePredictionResolver reads, and produces
    research_program-shaped resolution dicts so the inheritance rule
    (clamp_parent_confidence) can finally score claims that have no
    sportsbook. Read-only over ground truth: outcomes arrive explicitly
    (owner-entered, market-settled), never inferred from convenience.
    """

    name = "prediction_journal"

    def __init__(self, db, *, source_class: str = "PRIMARY"):
        """"db" is an aiosqlite connection. source_class is the provenance
        class of the RESOLVING evidence — ground truth observed directly is
        PRIMARY; downgrade only with a reason."""
        self._db = db
        self._source_class = source_class

    # ── claims ───────────────────────────────────────────────────────────

    async def create_claim(self, *, name: str, thesis: str,
                           notes: str = "") -> str:
        """Register a recurring hypothesis (lifecycle stage: draft).

        Idempotent on name — the lifecycle table keeps a UNIQUE index and
        re-registering returns the existing claim instead of duplicating.
        """
        if not (name or "").strip():
            raise PredictionJournalError("claim name must be non-empty")
        if not (thesis or "").strip():
            raise PredictionJournalError(
                "claim thesis must be non-empty — what would confirm it?")
        model_config = {"domain": "general", "legacy": False}
        cid = uuid.uuid4().hex[:12]
        now = _utcnow().isoformat(timespec="seconds")
        try:
            await self._db.execute(
                "INSERT INTO hypotheses "
                "(hypothesis_id, name, thesis, sport, market_type, model_config, "
                " status, created_at, updated_at, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
                (cid, name.strip(), thesis.strip(), GENERAL_CLAIM_SPORT,
                 GENERAL_CLAIM_MARKET, json.dumps(model_config), now, now,
                 notes or ""),
            )
            await self._db.commit()
            return cid
        except Exception as e:
            if "unique" in str(e).lower():
                cur = await self._db.execute(
                    "SELECT hypothesis_id FROM hypotheses WHERE name = ?",
                    (name.strip(),))
                row = await cur.fetchone()
                if row:
                    return row[0]
            raise

    async def _require_claim(self, claim_id: str) -> dict:
        cur = await self._db.execute(
            "SELECT hypothesis_id, status FROM hypotheses WHERE hypothesis_id = ?",
            (claim_id,))
        row = await cur.fetchone()
        if not row:
            raise PredictionJournalError(
                f"claim '{claim_id}' does not exist — create it first "
                "(a prediction must attach to a registered claim)")
        return {"id": row[0], "status": row[1]}

    # ── predictions ──────────────────────────────────────────────────────

    async def record_prediction(self, *, claim_id: str, event_id: str,
                                predicted_prob: float, context_key=None,
                                due_at=None) -> int:
        """Commit a number BEFORE ground truth exists. Returns prediction id.

        Moves the claim draft → paper_trading (preregistered forward-
        testing started) exactly once, on first prediction.
        """
        await self._require_claim(claim_id)
        eid = (event_id or "").strip()
        if not eid:
            raise PredictionJournalError(
                "event_id must be non-empty — an unnamed event cannot "
                "resolve into evidence")
        try:
            p = float(predicted_prob)
        except (TypeError, ValueError) as exc:
            raise PredictionJournalError(
                f"predicted_prob must be a number, got {predicted_prob!r}"
            ) from exc
        if not (0.0 <= p <= 1.0):
            raise PredictionJournalError(
                f"predicted_prob {p} outside [0,1]")
        due_iso = None
        if due_at is not None:
            due_iso = _parse_when(due_at).isoformat(timespec="seconds")
        cur = await self._db.execute(
            "INSERT INTO predictions "
            "(claim_id, event_id, predicted_prob, context_key, due_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (claim_id, eid, p, context_key, due_iso,
             _utcnow().isoformat(timespec="seconds")),
        )
        await self._db.execute(
            "UPDATE hypotheses SET status = 'paper_trading', updated_at = ? "
            "WHERE hypothesis_id = ? AND status = 'draft'",
            (_utcnow().isoformat(timespec="seconds"), claim_id),
        )
        await self._db.commit()
        return cur.lastrowid

    async def open_predictions(self, claim_id: Optional[str] = None
                               ) -> list[dict]:
        """Unresolved predictions, soonest-due first — what needs grading."""
        sql = (
            "SELECT p.id, p.claim_id, p.event_id, p.predicted_prob, "
            "       p.context_key, p.due_at, p.created_at, h.name AS claim_name "
            "FROM predictions p LEFT JOIN hypotheses h "
            "  ON h.hypothesis_id = p.claim_id "
            "WHERE NOT EXISTS (SELECT 1 FROM outcomes o "
            "                  WHERE o.prediction_id = p.id)")
        params: tuple = ()
        if claim_id:
            sql += " AND p.claim_id = ?"
            params = (claim_id,)
        sql += " ORDER BY p.due_at IS NULL, p.due_at, p.id"
        cur = await self._db.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in await cur.fetchall()]

    # ── resolution ───────────────────────────────────────────────────────

    async def resolve_prediction(self, prediction_id: int, outcome_raw: str,
                                 payoff: Optional[float] = None,
                                 resolved_by: str = "owner") -> dict:
        """Record ground truth for one prediction. Explicit, never inferred.

        Unknown outcome tokens are REFUSED (fail closed), not coerced to
        indeterminate. A second resolution is refused unless identical.
        """
        token = _norm_outcome(outcome_raw or "")
        if token not in ("positive", "negative", "indeterminate"):
            raise PredictionJournalError(
                f"outcome {outcome_raw!r} is not resolvable — use yes/no/"
                "push (or won/lost/true/false/hit/miss/retracted)")
        cur = await self._db.execute(
            "SELECT o.resolved_outcome FROM outcomes o "
            "WHERE o.prediction_id = ?", (prediction_id,))
        existing = await cur.fetchone()
        if existing:
            if existing[0] == token:
                return {"prediction_id": prediction_id,
                        "resolved_outcome": token, "idempotent": True}
            raise PredictionJournalError(
                f"prediction {prediction_id} already resolved as "
                f"{existing[0]!r}; refusing to re-resolve as {token!r}")
        if payoff is not None:
            try:
                payoff = float(payoff)
            except (TypeError, ValueError) as exc:
                raise PredictionJournalError(
                    f"payoff must be a number or omitted, got {payoff!r}"
                ) from exc
        await self._db.execute(
            "INSERT INTO outcomes "
            "(prediction_id, resolved_outcome, payoff, resolved_at, resolved_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (prediction_id, token, payoff,
             _utcnow().isoformat(timespec="seconds"), resolved_by),
        )
        await self._db.commit()
        return {"prediction_id": prediction_id,
                "resolved_outcome": token, "idempotent": False}

    # ── track record for the inheritance rule ────────────────────────────

    async def track_records(self, claim_id: str) -> list[dict]:
        """Resolution dicts in tools.research_program shape.

        positive→hit, negative→miss, indeterminate→void, unresolved past
        its own due_at→stale, unresolved before the deadline→excluded
        (nothing has settled yet). A prediction with NO deadline can never
        go stale — it simply waits excluded until an explicit outcome
        arrives; resolution scores whenever it happens, because the number
        was committed before ground truth either way.
        """
        await self._require_claim(claim_id)
        cur = await self._db.execute(
            "SELECT p.event_id, p.predicted_prob, p.due_at, p.created_at, "
            "       o.resolved_outcome, o.resolved_at, o.payoff "
            "FROM predictions p LEFT JOIN outcomes o "
            "  ON o.prediction_id = p.id WHERE p.claim_id = ?",
            (claim_id,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
        now = _utcnow()
        out: list[dict] = []
        for r in rows:
            raw = (r.get("resolved_outcome") or "").strip().lower()
            if raw:
                outcome = {"positive": "hit", "negative": "miss",
                           "indeterminate": "void"}.get(raw, "void")
                resolved_at = r.get("resolved_at") or r.get("created_at")
            else:
                due = _parse_when(r.get("due_at"))
                if due is None or due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc) if due else None
                if due is None or due > now:
                    continue          # not settled yet — honest exclusion
                outcome = "stale"     # unresolved by its own deadline
                resolved_at = r.get("due_at")
            if isinstance(resolved_at, str) and len(resolved_at) >= 10:
                resolved_at = resolved_at[:10]
            out.append({
                "question_id": r.get("event_id") or "",
                "outcome": outcome,
                "resolved_at": resolved_at,
                "pinball_score": None,
                "best_source_class": self._source_class,
            })
        return out

    async def track_summary(self, claim_id: str) -> dict:
        """Track record + inherited ceiling for one claim, ready to print."""
        from tools.research_program import (
            inherited_ceiling, summarize_track_record, tier_ceiling_from_score,
        )
        recs = await self.track_records(claim_id)
        tr = summarize_track_record(recs)
        ceil_ = inherited_ceiling(recs)
        return {
            "n_resolved": tr.n_resolved,
            "n_hit": tr.n_hit,
            "n_stale": tr.n_stale,
            "brier": tr.brier,
            "inherited_ceiling": ceil_,
            "ceiling_tier": tier_ceiling_from_score(ceil_),
        }
