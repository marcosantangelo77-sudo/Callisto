"""Source registry — the catalog the decomposer selects from.

Holds every registered SourceAdapter (spec + client + adapter), answers
"which sources can answer this kind of question", and exposes each
source's honest coverage limits. Selection by tier is enforced upstream
by the provenance ledger; this registry's job is discovery, not trust.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from tools.sources.base import PROVENANCE_TIERS, RestSource, SourceSpec

logger = logging.getLogger("callisto.source_registry")

_WORD_RE = re.compile(r"[a-z0-9]+")

# connectives that carry no topical meaning for source matching
_STOPWORDS = {"and", "the", "for", "with", "about", "into", "from",
              "what", "which", "that", "this", "data", "series"}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if len(w) >= 3]


def _overlap(q_words: list[str], answer_words: set[str]) -> tuple[bool, float, list[str]]:
    """(matches, score 0..1, matched question words). A question word
    matches when it equals or prefix-shares a word of the answer clause
    ('macro' matches 'macroeconomic'). Score = matched / asked, so an
    answer clause that covers more of the question ranks higher."""
    matched = []
    for qw in q_words:
        for w in answer_words:
            if w and (w.startswith(qw) or qw.startswith(w)):
                matched.append(qw)
                break
    if not q_words:
        return False, 0.0, []
    return (len(matched) == len(q_words), len(matched) / len(q_words),
            matched)


@dataclass
class SelectionDecision:
    """Why one source was chosen or skipped — the explainability contract
    the pipeline consumes. Every registered source gets one."""
    name: str
    included: bool
    score: float                 # relevance 0..1 (0.0 when skipped)
    reasons: list[str] = field(default_factory=list)
    spec: Optional[SourceSpec] = None

    def to_dict(self) -> dict:
        d = {"name": self.name, "included": self.included,
             "score": round(self.score, 3), "reasons": list(self.reasons)}
        if self.spec is not None:
            d["tier"] = self.spec.tier
        return d


@dataclass
class SourceAdapter:
    spec: SourceSpec
    make_adapter: object  # callable(RestSource) -> adapter instance


class SourceRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}

    def register(self, adapter: SourceAdapter) -> None:
        logger.info("source registry: registering '%s' (tier %d)",
                    adapter.spec.name, adapter.spec.tier)
        self._adapters[adapter.spec.name] = adapter

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def specs(self) -> list[dict]:
        return [a.spec.to_dict() for a in self._adapters.values()]

    def get(self, name: str) -> Optional[SourceAdapter]:
        return self._adapters.get(name)

    def select(self, question_type: str, *, max_tier: int = 5,
               exclude: set[str] | None = None,
               min_score: float = 0.34) -> list[SourceSpec]:
        """Specs whose `answers` overlap *question_type* on significant
        words (>=3-char tokens, prefix match so 'macro' matches
        'macroeconomic'), within a provenance-tier ceiling.
        Exclusions let callers drop sources that already failed.
        Ranked by relevance score, tie-broken by provenance tier."""
        return [d.spec for d in self.select_explained(
            question_type, max_tier=max_tier, exclude=exclude,
            min_score=min_score)
            if d.included and d.spec is not None]

    def select_explained(self, question_type: str, *, max_tier: int = 5,
                         exclude: set[str] | None = None,
                         min_score: float = 0.34,
    ) -> list[SelectionDecision]:
        """The explainable form of select(): EVERY registered source gets
        a SelectionDecision saying why it was included (with its relevance
        score) or skipped (with the reason). The pipeline surfaces these
        so a conclusion can state which sources bore on it and why the
        rest were ignored — no silent drops at ~20 sources.

        Scoring: an answer clause scores the fraction of the question's
        topical words (stopwords stripped) its tokens prefix-match.
        A source is included when its BEST clause covers >= min_score of
        those words — partial coverage still includes, because a source
        answering only 'prices' genuinely bears on 'energy prices
        inventories' even if it cannot answer the rest. Ranking among
        included: higher coverage first, tie-broken by provenance tier
        (lower = stronger evidence).
        """
        exclude = exclude or set()
        q_words = _tokens(question_type)
        decisions: list[SelectionDecision] = []
        for a in self._adapters.values():
            if a.spec.name in exclude:
                decisions.append(SelectionDecision(
                    a.spec.name, False, 0.0,
                    ["excluded by caller"], None))
                continue
            if a.spec.tier > max_tier:
                decisions.append(SelectionDecision(
                    a.spec.name, False, 0.0,
                    [f"tier {a.spec.tier} exceeds ceiling {max_tier}"],
                    None))
                continue
            if not q_words:
                decisions.append(SelectionDecision(
                    a.spec.name, False, 0.0,
                    ["question has no matchable words"], None))
                continue
            best: tuple[bool, float] = (False, 0.0)
            for ans in a.spec.answers:
                # score each answer clause against the question MINUS pure
                # connectives ('and', 'for', 'the'...) so 'GDP and trade'
                # is judged on GDP/trade alone
                core = [w for w in q_words if w not in _STOPWORDS] or q_words
                ok, score, matched = _overlap(core, set(_tokens(ans)))
                if score > best[1]:
                    best = (ok, score)
            ok_any, best_score = best
            if not ok_any and best_score < min_score:
                decisions.append(SelectionDecision(
                    a.spec.name, False, 0.0,
                    [f"best answer clause covers only "
                     f"{best_score:.0%} of the question's topical words"],
                    a.spec))
                continue
            decisions.append(SelectionDecision(
                a.spec.name, True, best_score,
                [f"answers clause covers {best_score:.0%} of the question; "
                 f"tier {a.spec.tier} ({PROVENANCE_TIERS.get(a.spec.tier, '?')})"],
                a.spec))
        included = [d for d in decisions if d.included]
        included.sort(key=lambda d: (-d.score, d.spec.tier if d.spec else 9,
                                     d.name))
        skipped = [d for d in decisions if not d.included]
        return included + skipped

    # ── instantiation ────────────────────────────────────────────────────

    def instantiate(self, name: str, ledger=None):
        """Build a live adapter for *name* with the process-wide ledger."""
        entry = self._adapters.get(name)
        if entry is None:
            raise KeyError(f"unknown source {name!r}")
        source = RestSource(entry.spec, ledger=ledger)
        return entry.make_adapter(source)


_default_registry: Optional[SourceRegistry] = None


def get_source_registry() -> SourceRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = SourceRegistry()
        from tools.sources import adapters as _all

        _all.register_all(_default_registry)
    return _default_registry
