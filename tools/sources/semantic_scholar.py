"""Semantic Scholar Graph API — papers beyond OpenAlex, with citation
graph + TLDRs. Tier 2.

api.semanticscholar.org/graph/v1. Key optional (public rate: shared,
unauthenticated ~1 req/s bursty and often throttled; with free key 1 rps
guaranteed) — set CALLISTO_S2_API_KEY; header x-api-key. We self-limit to
1 req/s either way.

Complements OpenAlex: citation INTENTS (methodology vs background),
influential citation counts, TLDR abstract summaries, paper
recommendations. Use OpenAlex for breadth/metadata; S2 for graph signals.

Answers: paper lookup by S2 id / DOI / arXiv id, citation contexts and
intents, influential-citation counts, author h-index records.
Cannot answer: full texts beyond open-access PDF links, peer-review
records, real-time indexing (lag days), guaranteed recall of very new
papers.
"""

from __future__ import annotations

import os

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="semanticscholar",
    base_url="https://api.semanticscholar.org/graph/v1",
    description="Semantic Scholar: citation graphs, intents, TLDRs",
    answers=(
        "paper search and lookup by DOI/arXiv/S2 id",
        "citation intent classification (method/background/result)",
        "influential-citation counts and TLDRs",
        "author metrics (h-index, paper counts)",
    ),
    cannot_answer=(
        "paywalled full texts (OA links only)",
        "peer-review records",
        "same-day indexing of brand-new papers",
    ),
    tier=2,
    # Live-verified 2026-08: unauthenticated requests sit in a shared
    # global pool and 429 near-constantly (not an IP block — the body
    # says "apply for a key for higher rate limits"). 1 req/s still 429s
    # in practice; back off to one call every 5s and treat key as the
    # real fix.
    min_interval_s=5.0,
    headers=(("x-api-key", "{api_key}"),),
    terms_url="https://www.semanticscholar.org/product/api",
    key_env_var="CALLISTO_S2_API_KEY",
)

_FIELDS = ("title,year,abstract,citationCount,influentialCitationCount,"
           "externalIds,openAccessPdf,tldr")


class SemanticScholarAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def _params(self, params: dict) -> dict:
        params = dict(params)
        key = self.source.api_key()
        if key:
            params["apiKeyPresent"] = "1"  # key travels in header instead
        return params

    def paper_search(self, query: str, limit: int = 10) -> dict:
        url = self.source.build_url(
            "/paper/search", self._params({"query": query,
                                           "limit": max(1, min(limit, 100)),
                                           "fields": _FIELDS}))
        return self.source.get_json(url)[0]

    def paper(self, paper_id: str) -> dict:
        """paper_id: S2 id, 'DOI:10.xxxx/...', or 'arXiv:2101.00000'."""
        pid = paper_id.strip()
        if not pid:
            raise ValueError("empty paper id")
        url = self.source.build_url(f"/paper/{pid}",
                                    self._params({"fields": _FIELDS}))
        return self.source.get_json(url)[0]

    def citations(self, paper_id: str, limit: int = 50) -> dict:
        url = self.source.build_url(
            f"/paper/{paper_id.strip()}/citations",
            self._params({"limit": max(1, min(int(limit), 1000)),
                          "fields": "title,intents,isInfluential"}))
        return self.source.get_json(url)[0]
