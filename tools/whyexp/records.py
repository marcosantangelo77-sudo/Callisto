"""Explanation record dataclasses for tools.whyexp."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SCHEMA_VERSION = 1

_CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}

# Requirement-gate cap applied by tools/pipeline/engine.py when a leaf's
# evidence requirements are unmet (engine.py: min(clamped, 0.54)).
REQUIREMENT_GATE_CAP = 0.54


# ═════════════════════════════════════════════════════════════════════════
# Explanation records
# ═════════════════════════════════════════════════════════════════════════


@dataclass
class EvidenceWhy:
    """One evidence item: the class provenance assigned it, and WHY."""
    label: str                       # short identifier (source name / index)
    source_class: str
    reason: str                      # the provenance rule that fired
    ceiling: float                   # confidence ceiling implied by the class

    def to_dict(self) -> dict:
        return {"label": self.label, "source_class": self.source_class,
                "reason": self.reason, "ceiling": self.ceiling}


@dataclass
class CeilingWhy:
    """One constraint that bounded the score."""
    kind: str            # source_class | requirement_gate | inheritance | ...
    value: Optional[float]   # the numeric ceiling (None for vetoes/floors)
    detail: str          # plain-language why
    binding: bool = False

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value,
                "detail": self.detail, "binding": self.binding}


@dataclass
class ObjectionWhy:
    """One adversary objection and what it cost."""
    text: str
    kind: str
    severity: str
    penalty: float
    veto: bool
    status: str = ""

    def to_dict(self) -> dict:
        return {"text": self.text, "kind": self.kind, "severity": self.severity,
                "penalty": self.penalty, "veto": self.veto, "status": self.status}


@dataclass
class IndependenceWhy:
    """How many INDEPENDENT sources there were, and which families collapsed."""
    n_fetches: int
    independent_keys: list[str]
    n_independent: int
    collapses: list[str]     # human-readable family-collapse statements

    def to_dict(self) -> dict:
        return {"n_fetches": self.n_fetches,
                "independent_keys": self.independent_keys,
                "n_independent": self.n_independent,
                "collapses": self.collapses}


@dataclass
class RejectedWhy:
    """A fetch rejected at ingestion, and for what reason."""
    source_name: str
    url: str
    reason: str
    relevance_score: float = -1.0
    content_sha256: str = ""

    def to_dict(self) -> dict:
        return {"source_name": self.source_name, "url": self.url,
                "reason": self.reason, "relevance_score": self.relevance_score,
                "content_sha256": self.content_sha256}


@dataclass
class StepWhy:
    """One arithmetic step in the walk from proposal to final score."""
    stage: str
    before: float
    after: float
    rule: str

    @property
    def drop(self) -> float:
        return round(self.before - self.after, 4)

    def to_dict(self) -> dict:
        return {"stage": self.stage, "before": self.before,
                "after": self.after, "rule": self.rule, "drop": self.drop}
