"""Source registry — the catalog the decomposer selects from.

Holds every registered SourceAdapter (spec + client + adapter), answers
"which sources can answer this kind of question", and exposes each
source's honest coverage limits. Selection by tier is enforced upstream
by the provenance ledger; this registry's job is discovery, not trust.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from tools.sources.base import PROVENANCE_TIERS, RestSource, SourceSpec

logger = logging.getLogger("callisto.source_registry")


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
               exclude: set[str] | None = None) -> list[SourceSpec]:
        """Specs whose `answers` overlap *question_type* on significant
        words (>=3-char tokens, prefix match so 'macro' matches
        'macroeconomic'), within a provenance-tier ceiling.
        Exclusions let callers drop sources that already failed."""
        exclude = exclude or set()
        q_words = [w for w in
                   "".join(c if c.isalnum() else " " for c in question_type.lower()).split()
                   if len(w) >= 3]
        out = []
        for a in self._adapters.values():
            if a.spec.name in exclude or a.spec.tier > max_tier:
                continue
            for ans in a.spec.answers:
                words = set("".join(
                    c if c.isalnum() else " " for c in ans.lower()).split())
                if q_words and all(
                        any(w.startswith(qw) or qw.startswith(w) and w
                            for w in words) for qw in q_words):
                    out.append(a.spec)
                    break
        out.sort(key=lambda s: s.tier)
        return out

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
