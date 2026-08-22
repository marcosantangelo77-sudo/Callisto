"""OpenAlex — 250M+ scholarly works, fully open, no key. Tier 2 (documents).

api.openalex.org. Polite pool: set CALLISTO_OPENALEX_EMAIL (mailto in UA /
query param) to get the polite pool and faster service.
https://docs.openalex.org — 100k calls/day polite ceiling; we self-limit
to 5 req/s.

Answers: scholarly works by title/author/concept search, citation counts,
institutions, authors, open-access locations.
Cannot answer: paywalled full texts (metadata + OA links only), peer
review records, preprint peer status, real-time publication (index lag
can be hours to days).
"""

from __future__ import annotations

import os
from typing import Any

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="openalex",
    base_url="https://api.openalex.org",
    description="Open catalog of 250M+ scholarly works, authors, institutions",
    answers=(
        "scholarly work search by title/author/topic",
        "citation counts and reference lists",
        "author and institution records",
        "open-access locations for a paper",
    ),
    cannot_answer=(
        "paywalled full texts (metadata and OA links only)",
        "peer-review records or acceptance status",
        "same-day indexing (lag can be hours to days)",
    ),
    tier=2,
    min_interval_s=0.2,
    terms_url="https://docs.openalex.org/#api-terms-of-service",
)


class OpenAlexAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def _params(self, params: dict) -> dict:
        params = dict(params)
        mail = os.environ.get("CALLISTO_OPENALEX_EMAIL", "")
        if mail:
            params["mailto"] = mail
        return params

    def works_search(self, query: str, limit: int = 10) -> dict:
        url = self.source.build_url(
            "/works", self._params({"search": query, "per-page": min(int(limit), 200)}))
        return self.source.get_json(url)[0]

    def work(self, openalex_id: str) -> dict:
        wid = openalex_id.rsplit("/", 1)[-1]
        url = self.source.build_url(f"/works/{wid}", self._params({}))
        return self.source.get_json(url)[0]

    def filter_works(self, filt: str, limit: int = 25) -> dict:
        """Filter syntax per docs.openalex.org, e.g.
        'primary_topic.id:T12345,publication_year:2024'."""
        url = self.source.build_url(
            "/works", self._params({"filter": filt, "per-page": min(int(limit), 200)}))
        return self.source.get_json(url)[0]
