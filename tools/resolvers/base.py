"""OutcomeResolver interface: the seam between the hypothesis lifecycle and
any domain's ground truth.

Design (DOMAIN_GENERALITY.md §3b, BUILD_MANDATE queue item 1):

    EvidenceRecord = one resolved prediction-vs-outcome observation
    OutcomeResolver(hypothesis) -> stream of EvidenceRecords

The record shape is deliberately minimal and domain-free:

    event_id        stable identifier of the predicted event
    predicted_prob  the claim's probability for that event, at prediction time
    resolved_outcome  "positive" | "negative" | "indeterminate"
                      (generalises won/lost/push; indeterminate ≈ push)
    payoff          per-unit return on a unit stake if the claim were acted on
                    (0.0 for indeterminate; may be None when payoff is not a
                    meaningful concept — e.g. a retracted-paper prediction)
    context_key     free-form regime/context bucket for diversity checks
    resolved_at     ISO timestamp of ground-truth arrival

Sports stays green by construction: BettingOutcomeResolver is the first
implementation and maps its vocabulary onto this shape; nothing existing
changes behavior unless a caller opts into the resolver path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable, Optional

# Generalised outcome vocabulary. The betting strings map:
#   "won" -> POSITIVE, "lost" -> NEGATIVE, "push" -> INDETERMINATE.
OUTCOME_POSITIVE = "positive"
OUTCOME_NEGATIVE = "negative"
OUTCOME_INDETERMINATE = "indeterminate"

BETTING_OUTCOME_MAP = {
    "won": OUTCOME_POSITIVE,
    "lost": OUTCOME_NEGATIVE,
    "push": OUTCOME_INDETERMINATE,
}

# Stage semantics: storage name -> general meaning. The DB column values do
# NOT change (every reader in the repo depends on them); this mapping is what
# new, domain-general code should present to users.
STAGE_SEMANTICS = {
    "draft": "draft",
    "backtesting": "retrospective_testing",
    "paper_trading": "preregistered_forward_testing",
    "live": "deployed_conclusion",
    "retired": "retired",
}


@dataclass
class EvidenceRecord:
    """One resolved prediction-vs-outcome observation."""

    event_id: str
    predicted_prob: Optional[float]
    resolved_outcome: str          # one of OUTCOME_*
    resolved_at: Optional[str] = None
    payoff: Optional[float] = None       # per-unit return; None if not applicable
    odds_american: Optional[int] = None  # optional market price at prediction
    model_fair_prob: Optional[float] = None
    book_implied_prob: Optional[float] = None
    clv_prob_bp: Optional[float] = None  # canonical devigged CLV, basis points
    context_key: Optional[str] = None
    source: str = ""                     # which resolver produced it

    @property
    def is_decided(self) -> bool:
        return self.resolved_outcome in (OUTCOME_POSITIVE, OUTCOME_NEGATIVE)

    @property
    def binary_outcome(self) -> Optional[int]:
        """1/0 for decided records; None for indeterminate."""
        if not self.is_decided:
            return None
        return 1 if self.resolved_outcome == OUTCOME_POSITIVE else 0

    @classmethod
    def from_betting_row(cls, row: dict, source: str = "betting") -> "EvidenceRecord":
        """Adapt a backtest_events / paper_trades row onto the general shape."""
        outcome = BETTING_OUTCOME_MAP.get(
            (row.get("actual_result") or "").strip().lower(),
            OUTCOME_INDETERMINATE,
        )
        return cls(
            event_id=str(row.get("event_id") or row.get("trade_id") or ""),
            predicted_prob=row.get("model_fair_prob"),
            resolved_outcome=outcome,
            resolved_at=row.get("created_at"),
            payoff=row.get("hypothetical_pnl"),
            odds_american=row.get("book_odds_american")
            or row.get("signal_odds_american"),
            model_fair_prob=row.get("model_fair_prob"),
            book_implied_prob=row.get("book_implied_prob")
            or row.get("signal_implied_prob"),
            clv_prob_bp=row.get("clv_prob_bp"),
            context_key="|".join(
                str(row.get(k)) for k in ("game_date", "home_team", "away_team")
                if row.get(k)
            ),
            source=source,
        )


@dataclass
class ResolutionSummary:
    """Aggregate answer to 'has this claim resolved, and how did it score?'."""

    total: int = 0
    positive: int = 0
    negative: int = 0
    indeterminate: int = 0
    unresolved: int = 0
    hit_rate: float = 0.0               # positives / decided
    avg_payoff: Optional[float] = None
    avg_clv_prob_bp: Optional[float] = None
    positive_clv_rate: Optional[float] = None
    fully_resolved: bool = False
    details: dict = field(default_factory=dict)

    @classmethod
    def from_records(cls, records: list[EvidenceRecord]) -> "ResolutionSummary":
        s = cls(total=len(records))
        decided = 0
        payoffs: list[float] = []
        clvs: list[float] = []
        for r in records:
            if r.resolved_outcome == OUTCOME_POSITIVE:
                s.positive += 1
                decided += 1
            elif r.resolved_outcome == OUTCOME_NEGATIVE:
                s.negative += 1
                decided += 1
            elif r.resolved_outcome == OUTCOME_INDETERMINATE:
                s.indeterminate += 1
            else:
                s.unresolved += 1
            if r.payoff is not None:
                payoffs.append(r.payoff)
            if r.clv_prob_bp is not None:
                clvs.append(r.clv_prob_bp)
        s.hit_rate = s.positive / decided if decided else 0.0
        if payoffs:
            s.avg_payoff = sum(payoffs) / len(payoffs)
        if clvs:
            s.avg_clv_prob_bp = sum(clvs) / len(clvs)
            s.positive_clv_rate = sum(1 for c in clvs if c > 0) / len(clvs)
        s.fully_resolved = s.unresolved == 0 and s.total > 0
        return s


# Inverse of BETTING_OUTCOME_MAP: general vocabulary onto the row shape
# evaluate_significance() has always consumed.
GENERAL_OUTCOME_TO_BETTING = {
    OUTCOME_POSITIVE: "won",
    OUTCOME_NEGATIVE: "lost",
    OUTCOME_INDETERMINATE: "push",
}


def evidence_records_to_eval_rows(
    records: Iterable[EvidenceRecord],
) -> list[dict]:
    """Map EvidenceRecords onto the betting-row dict shape that
    HypothesisManager.evaluate_significance() consumes, so every existing
    statistic (binomial p, Brier, IC, Sharpe, CLV rate, calibration bins)
    runs unchanged over any domain's evidence.

    Honesty rules:

    * ``edge``/``ev_pct`` are computed only where BOTH the claim's own
      probability and a recorded market-implied probability exist — never
      fabricated.
    * ``clv_implied`` carries clv_prob_bp converted to a 0..1-scale rate
      (basis points / 10_000), matching the legacy clv_implied units the
      report averages.
    * ``book_odds_american`` and ``payoff`` pass through as-is; rows without
      either contribute no return observation rather than an invented one.
    """
    rows: list[dict] = []
    for rec in records:
        outcome = GENERAL_OUTCOME_TO_BETTING.get(rec.resolved_outcome)
        if outcome is None:
            outcome = "unresolved"
        prob = (
            rec.predicted_prob
            if rec.predicted_prob is not None
            else rec.model_fair_prob
        )
        row: dict = {"actual_result": outcome}
        if prob is not None:
            row["model_fair_prob"] = prob
        if rec.book_implied_prob is not None:
            row["book_implied_prob"] = rec.book_implied_prob
        if rec.odds_american is not None:
            row["book_odds_american"] = int(rec.odds_american)
        if rec.payoff is not None:
            row["payoff"] = float(rec.payoff)
        if rec.clv_prob_bp is not None:
            row["clv_implied"] = rec.clv_prob_bp / 10000.0
        if prob is not None and rec.book_implied_prob is not None:
            row["edge"] = prob - rec.book_implied_prob
            if rec.odds_american is not None:
                try:
                    from tools.math_utils import american_to_decimal

                    dec = american_to_decimal(int(rec.odds_american))
                    row["ev_pct"] = (prob * dec - 1.0) * 100.0
                except Exception:
                    # A malformed price must not fabricate an EV number.
                    pass
        if rec.context_key is not None:
            row["context_key"] = rec.context_key
        rows.append(row)
    return rows


def _norm_outcome(raw: str) -> str:
    """Normalise any domain's outcome token onto the general vocabulary."""
    t = raw.strip().lower()
    if t in ("won", "win", "positive", "true", "yes", "1", "hit", "confirmed"):
        return OUTCOME_POSITIVE
    if t in ("lost", "loss", "negative", "false", "no", "0", "miss", "retracted"):
        return OUTCOME_NEGATIVE
    if t in ("push", "indeterminate", "void", "cancelled", "n/a"):
        return OUTCOME_INDETERMINATE
    return "unresolved"


class OutcomeResolver(ABC):
    """Answers: has this claim resolved, and how did it score?

    Implementations are read-only adapters over their domain's ground truth.
    A Bitcoin hash-rate claim, a materials-science prediction, and an NBA
    spread all implement this same surface; the lifecycle machinery above it
    never sees a domain noun.
    """

    #: short id used in provenance fields ("betting", "literature", ...)
    name: str = "abstract"

    @abstractmethod
    async def iter_evidence(self, hypothesis_id: str) -> AsyncIterator[EvidenceRecord]:
        """Yield resolved (and pending, flagged via unresolved) evidence."""

    async def summarize(self, hypothesis_id: str) -> ResolutionSummary:
        records = [r async for r in self.iter_evidence(hypothesis_id)]
        return ResolutionSummary.from_records(records)

    async def has_resolved(self, hypothesis_id: str, min_n: int = 1) -> bool:
        n = 0
        async for r in self.iter_evidence(hypothesis_id):
            if r.is_decided:
                n += 1
                if n >= min_n:
                    return True
        return False
