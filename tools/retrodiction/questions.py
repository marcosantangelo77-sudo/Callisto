"""Retrodiction question generation and storage.

A retrodiction question is a dated, RESOLVED claim: the answer was unknown at
`claim_date` and is known at `resolution_date`. The answer lives in the record
but is structurally separated from what a researcher run can see — a researcher
only ever receives the question text, the claim date, and (optionally) its own
prior runs' outputs. Answers are read back only by the scorer.

Domain-general: financial generators are one family; any generator that emits
RetrodictionQuestion records plugs in identically. No domain vocabulary lives
in this module's types.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path


class QuestionType(str, Enum):
    BEAT_OR_MISS = "beat_or_miss"        # did X beat consensus Y?
    GUIDANCE_CHANGE = "guidance_change"  # was guidance raised/lowered?
    EVENT_OUTCOME = "event_outcome"      # did event E happen by date D?
    THRESHOLD_CROSS = "threshold_cross"  # did quantity cross value V by date D?


@dataclass
class RetrodictionQuestion:
    """One resolvable historical question. `answer_*` fields are the held-out
    half: never serialized into anything a Researcher sees."""
    question_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    text: str = ""
    domain: str = "GENERAL"
    question_type: QuestionType = QuestionType.EVENT_OUTCOME
    # The "as of" date — evidence must be published strictly before this.
    claim_date: Optional[date] = None
    resolution_date: Optional[date] = None
    horizon_days: int = 0              # claim→resolve span, for slicing
    # Held out from the pipeline:
    answer_binary: bool = False        # e.g. True = beat / raised / happened
    answer_confidence: float = 1.0     # how certain history is about it
    # Market-implied fair probability at claim time (devigged where the
    # source allows). Optional: None means "no market existed for this
    # claim" and magnitude scoring is skipped. NEXT.md RETRODICTION
    # SCORING: continuous market-relative scoring carries far more power
    # per observation than binary Brier — but only when the benchmark is
    # actually carried on the question. Before this field existed the
    # batch runner did getattr(q, 'market_implied', None) and every real
    # question silently scored binary-only.
    market_implied: Optional[float] = None

    def __post_init__(self):
        if self.market_implied is not None:
            if not isinstance(self.market_implied, (int, float)) or \
                    isinstance(self.market_implied, bool) or \
                    not (0.0 <= self.market_implied <= 1.0):
                raise ValueError(
                    f"market_implied must be a probability in [0,1], "
                    f"got {self.market_implied!r}")
        if self.claim_date and self.resolution_date:
            if self.resolution_date <= self.claim_date:
                raise ValueError("resolution_date must be after claim_date")
            self.horizon_days = (self.resolution_date - self.claim_date).days

    def validate(self) -> list[str]:
        errs = []
        if not self.text.strip():
            errs.append(f"{self.question_id}: empty text")
        if self.claim_date is None or self.resolution_date is None:
            errs.append(f"{self.question_id}: missing dates")
        elif self.answer_confidence < 0.5:
            errs.append(f"{self.question_id}: ambiguous ground truth "
                        f"(confidence {self.answer_confidence})")
        return errs

    def prompt_for_researcher(self) -> dict:
        """Exactly what a researcher run may see. The answer is not in it."""
        return {
            "question_id": self.question_id,
            "text": self.text,
            "domain": self.domain,
            "question_type": self.question_type.value,
            "claim_date": self.claim_date.isoformat(),
        }


# ── Generators ────────────────────────────────────────────────────────────

def generate_earnings_questions(earnings_history) -> list[RetrodictionQuestion]:
    """Build beat/miss questions from an earnings-history fixture.

    `earnings_history`: iterable of dicts with keys
      ticker, report_date (date), eps_actual (float), eps_consensus (float).
      Optional key `market_implied` — the devigged fair probability of a
      beat implied by the options/prediction market at claim time. When
      present it rides on the question so magnitude scoring has its
      benchmark (NEXT.md RETRODICTION SCORING).
    For each report, a question asked as of 30 days before the report:
    'will X beat consensus EPS next report?' Answer: actual > consensus.

    Fixture-driven only — no network, no API.
    """
    out: list[RetrodictionQuestion] = []
    for row in earnings_history:
        report = _as_date(row["report_date"])
        claim = date.fromordinal(report.toordinal() - 30)
        beat = float(row["eps_actual"]) > float(row["eps_consensus"])
        out.append(RetrodictionQuestion(
            text=(f"Will {row['ticker']} report EPS above consensus in its "
                  f"next earnings report?"),
            domain="FINANCIAL",
            question_type=QuestionType.BEAT_OR_MISS,
            claim_date=claim,
            resolution_date=report,
            answer_binary=beat,
            answer_confidence=1.0,
            market_implied=(float(row["market_implied"])
                            if row.get("market_implied") is not None else None),
        ))
    return out


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


# ── Persistence ───────────────────────────────────────────────────────────

def save_questions(questions, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for q in questions:
        d = asdict(q)
        d["question_type"] = q.question_type.value
        d["claim_date"] = q.claim_date.isoformat() if q.claim_date else None
        d["resolution_date"] = (q.resolution_date.isoformat()
                                if q.resolution_date else None)
        payload.append(d)
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_questions(path) -> list[RetrodictionQuestion]:
    raw = json.loads(Path(path).read_text())
    out = []
    for d in raw:
        d["question_type"] = QuestionType(d["question_type"])
        d["claim_date"] = date.fromisoformat(d["claim_date"]) if d["claim_date"] else None
        d["resolution_date"] = (date.fromisoformat(d["resolution_date"])
                                if d["resolution_date"] else None)
        out.append(RetrodictionQuestion(**{k: v for k, v in d.items()
                                           if k != "horizon_days"}))
    return out
