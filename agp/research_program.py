"""
ResearchProgram — the missing first-class object (BUILD_MANDATE §3 item 5).

A ResearchProgram turns one root query into a TREE of sub-questions, each
with its own scope, evidence requirements, resolution horizon, and status.
The tree IS the research plan and the audit trail at once.

Design constraints honored here:
  - Zero domain vocabulary. This works identically for a Bitcoin thesis,
    a protein-folding prediction, and a supply-chain claim. There is no
    sport, market, or ticker anywhere in this module.
  - Evidence requirements are gates, not prose: each question carries
    min_source_class / min_independent_sources / quant_required, meant to
    be consumed by the EXISTING provenance + clamp machinery
    (agp/provenance.py, agp/thresholds.py MAX_CONFIDENCE_BY_SOURCE).
  - Quantile commitments are stored as distributions (P10/P50/P90 per
    horizon date) so partial outcomes score CONTINUOUSLY (pinball loss)
    instead of pass/fail at a single far-off settlement date. That is the
    storage half of the horizon problem; the confidence-inheritance half
    lives in tools/research_program.py.

Nothing here touches the live execution path or weakens any gate.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Optional


# ── Enums ────────────────────────────────────────────────────────────────

class QuestionKind(str, Enum):
    DESCRIPTIVE = "descriptive"    # what is true now / what was true
    CAUSAL = "causal"              # does X drive Y
    PREDICTIVE = "predictive"      # will X be true by date D


class QuestionStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    FALSIFIED = "falsified"
    UNRESOLVABLE = "unresolvable"


class ProgramStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CONCLUDED = "concluded"


class SourceClassRank(str, Enum):
    """Ordered evidence-authority classes, mirroring agp.SourceClass values
    without importing betting-adjacent machinery. Lowest rank = weakest."""
    INFERRED = "INFERRED"
    SIGNAL = "SIGNAL"
    SECONDARY = "SECONDARY"
    PRIMARY = "PRIMARY"

    @property
    def rank(self) -> int:
        return _CLASS_ORDER.index(self)

    @classmethod
    def min_of(cls, *values: str) -> "SourceClassRank":
        best = None
        for v in values:
            if v is None:
                continue
            try:
                r = cls(v)
            except ValueError:
                continue
            if best is None or r.rank < best.rank:
                best = r
        return best if best is not None else cls.INFERRED


_CLASS_ORDER = ["INFERRED", "SIGNAL", "SECONDARY", "PRIMARY"]


MAX_TREE_DEPTH = 3


# ── Evidence requirements (gates, not prose) ────────────────────────────

@dataclass
class EvidenceRequirement:
    """What it takes for this question to seal at meaningful confidence.

    Consumed by the existing clamp machinery: if the session's
    provenance-assigned best source class never reached ``min_source_class``,
    or fewer than ``min_independent_sources`` independent sources arrived,
    the leaf cannot seal above its requirement-floor.
    """
    min_source_class: SourceClassRank = SourceClassRank.SECONDARY
    min_independent_sources: int = 2
    quant_required: bool = False     # must produce numbers, not prose

    def validate(self) -> list[str]:
        errs = []
        if self.min_independent_sources < 1:
            errs.append("min_independent_sources must be >= 1")
        return errs

    def unmet_reasons(self, achieved_source_class: SourceClassRank,
                      independent_sources: int,
                      produced_quant: bool) -> list[str]:
        """Reasons this requirement is currently unmet (empty = met)."""
        reasons = []
        if achieved_source_class.rank < self.min_source_class.rank:
            reasons.append(
                f"best evidence class {achieved_source_class.value} < "
                f"required {self.min_source_class.value}")
        if independent_sources < self.min_independent_sources:
            reasons.append(
                f"{independent_sources} independent sources < required "
                f"{self.min_independent_sources}")
        if self.quant_required and not produced_quant:
            reasons.append("question requires quantitative support")
        return reasons


# ── Horizon ──────────────────────────────────────────────────────────────

@dataclass
class Horizon:
    """A dated window. Every PREDICTIVE question MUST carry one: an
    undated prediction cannot enter the lifecycle, because nothing
    resolves and nothing scores."""
    claim_date: date
    resolve_date: date

    def validate(self) -> list[str]:
        errs = []
        if self.resolve_date <= self.claim_date:
            errs.append("resolve_date must be after claim_date")
        return errs

    @property
    def span_days(self) -> int:
        return (self.resolve_date - self.claim_date).days


# ── Questions and programs ───────────────────────────────────────────────

@dataclass
class ResearchQuestion:
    text: str
    kind: QuestionKind
    priority: float = 0.5                       # 0..1
    evidence_requirements: EvidenceRequirement = field(
        default_factory=EvidenceRequirement)
    horizon: Optional[Horizon] = None
    children: list["ResearchQuestion"] = field(default_factory=list)
    status: QuestionStatus = QuestionStatus.OPEN
    # Optional bridge into the existing lifecycle (hypothesis/paper-trade id)
    lifecycle_link: Optional[str] = None
    question_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def _depth(self) -> int:
        return 1 if not self.children else \
            1 + max(c._depth() for c in self.children)

    def validate(self) -> list[str]:
        """Structural validity errors. Empty list = valid node/subtree."""
        errs: list[str] = []
        if not (self.text or "").strip():
            errs.append(f"{self.question_id}: empty question text")
        if not (0.0 <= self.priority <= 1.0):
            errs.append(f"{self.question_id}: priority outside [0,1]")
        errs.extend(self.evidence_requirements.validate())
        if self.kind == QuestionKind.PREDICTIVE:
            if self.horizon is None:
                errs.append(
                    f"{self.question_id}: PREDICTIVE question lacks a horizon "
                    f"(undated predictions cannot resolve)")
            else:
                errs.extend(self.horizon.validate())
        if self._depth() > MAX_TREE_DEPTH:
            errs.append(f"{self.question_id}: tree deeper than {MAX_TREE_DEPTH}")
        seen = {self.question_id}
        for child in self.children:
            for e in child.validate():
                errs.append(e)
            if child.question_id in seen:
                errs.append(f"{child.question_id}: duplicate question id")
            seen.add(child.question_id)
        return errs

    # ── traversal ──

    def walk(self) -> Iterable["ResearchQuestion"]:
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def leaves(self) -> list["ResearchQuestion"]:
        return [q for q in self.walk() if not q.children]

    def find(self, question_id: str) -> Optional["ResearchQuestion"]:
        for q in self.walk():
            if q.question_id == question_id:
                return q
        return None


@dataclass
class ArtifactRef:
    """Pointer to a verifiable artifact (csv/png/json/ipynb...) backing an
    answer. Content-addressed so tampering breaks the reference."""
    kind: str
    sha256: str

    def __post_init__(self) -> None:
        if len(self.sha256) != 64:
            raise ValueError("artifact sha256 must be a 64-char hex digest")


@dataclass
class ResearchProgram:
    root_query: str
    domain: str = "GENERAL"          # mirrors agp.Domain values, no new vocab
    program_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    questions: list[ResearchQuestion] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    status: ProgramStatus = ProgramStatus.ACTIVE
    created_at: datetime = field(default_factory=datetime.utcnow)

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not (self.root_query or "").strip():
            errs.append("empty root_query")
        if len(self.questions) > MAX_TREE_DEPTH and any(
                q.children for q in self.questions):
            errs.append(f"tree deeper than {MAX_TREE_DEPTH}")
        ids: set[str] = set()
        for q in self.questions:
            errs.extend(q.validate())
            if q.question_id in ids:
                errs.append(f"{q.question_id}: duplicate question id")
            ids.add(q.question_id)
        return errs

    def walk_questions(self) -> Iterable[ResearchQuestion]:
        for q in self.questions:
            yield from q.walk()

    @property
    def leaves(self) -> list[ResearchQuestion]:
        return [q for q in self.walk_questions() if not q.children]

    def fingerprint(self) -> str:
        """Stable content hash of the program structure (audit trail anchor)."""
        payload = {
            "root_query": self.root_query,
            "domain": self.domain,
            "questions": [_q_to_dict(q) for q in self.questions],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _q_to_dict(q: ResearchQuestion) -> dict:
    return {
        "id": q.question_id,
        "text": q.text,
        "kind": q.kind.value,
        "priority": q.priority,
        "requirements": {
            "min_source_class": q.evidence_requirements.min_source_class.value,
            "min_independent_sources":
                q.evidence_requirements.min_independent_sources,
            "quant_required": q.evidence_requirements.quant_required,
        },
        "horizon": None if q.horizon is None else {
            "claim_date": q.horizon.claim_date.isoformat(),
            "resolve_date": q.horizon.resolve_date.isoformat(),
        },
        "children": [_q_to_dict(c) for c in q.children],
    }


# ── Quantile commitments: scored continuously, not at settlement ────────

QUANTILE_LEVELS = (0.10, 0.50, 0.90)


@dataclass
class QuantileForecast:
    """A calibrated distribution over an outcome value, stated AT ISSUE TIME.

    One forecast covers ONE horizon checkpoint (e.g., 'BTC price at
    2027-06-30'). A long-horizon target is a LIST of these, one per year —
    so reality can start scoring against them long before the final date.
    """
    horizon_date: date
    p10: float
    p50: float
    p90: float
    unit: str = ""                   # domain-general: USD, nm, days, ...

    def validate(self) -> list[str]:
        errs = []
        for name in ("p10", "p50", "p90"):
            v = getattr(self, name)
            if v != v or v in (float("inf"), float("-inf")):
                errs.append(f"{name} is not finite")
        if not (self.p10 <= self.p50 <= self.p90):
            errs.append("quantiles must be ordered p10 <= p50 <= p90")
        return errs

    def to_dict(self) -> dict:
        return {"horizon_date": self.horizon_date.isoformat(),
                "p10": self.p10, "p50": self.p50, "p90": self.p90,
                "unit": self.unit}


def pinball_loss(level: float, predicted: float, observed: float) -> float:
    """Quantile (pinball) loss — Brier's analogue for quantile forecasts.

    0 when the observed value lands exactly on the predicted quantile;
    asymmetric linear penalty otherwise. Lower is better; averaging across
    levels gives a strictly proper score for a quantile forecast.
    """
    diff = observed - predicted
    if diff >= 0:
        return level * diff
    return (level - 1.0) * diff   # == (1 - level) * |diff|


def score_quantile_forecast(forecast: QuantileForecast,
                            observed: float) -> float:
    """Mean pinball loss across the stored levels for one realized checkpoint.

    Defined the moment ANY checkpoint passes — continuous scoring instead
    of waiting for the final settlement date.
    """
    errs = forecast.validate()
    if errs:
        raise ValueError(f"invalid forecast: {errs}")
    preds = {"p10": 0.10, "p50": 0.50, "p90": 0.90}
    losses = [
        pinball_loss(level, getattr(forecast, attr), observed)
        for attr, level in preds.items()
    ]
    return sum(losses) / len(losses)


def scale_reference(values: Iterable[float]) -> float:
    """Baseline spread used to normalize pinball losses into a 0..1 skill
    score. Domain-general: uses the median absolute deviation of whatever
    historical values exist for the quantity being forecast."""
    vals = sorted(float(v) for v in values)
    if not vals:
        return 1.0
    mid = vals[len(vals) // 2] if len(vals) % 2 else \
        (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2.0
    abs_devs = sorted(abs(v - mid) for v in vals)
    mad = abs_devs[len(abs_devs) // 2]
    return mad if mad > 0 else 1.0
