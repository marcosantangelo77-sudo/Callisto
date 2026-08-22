"""CourtListener — Free Law Project's US case-law + PACER docket corpus. Tier 2.

www.courtlistener.com/api/rest/v4 (docs: courtlistener.com/help/api/rest).
Token auth REQUIRED for programmatic use: header `Authorization: Token <key>`;
set CALLISTO_COURTLISTENER_TOKEN. Current throttles are account-tiered and
low (a free account gets ~125 requests/day) — we self-limit to one request
every 3 seconds and cap page_size at 20 so a search stays inside quota.

Pagination convention: responses carry `count`, `next`, `previous`. `next`
is an opaque CURSOR url — always follow it verbatim rather than incrementing
`page=` yourself, because the underlying ordering can shift between requests.
paginate() does exactly that and yields result pages until exhausted or
`max_pages` is hit.

Answers: case-law opinion-cluster search (type=o), dockets (r/d), judges,
oral-argument audio, citation-lookup, cluster/opinion retrieval by id.
Cannot answer: PACER document BODIES (bills separately, needs receipts),
guaranteed completeness of very recent filings, non-US law, and the old
5,000/hr rate assumption — free tier quota makes crawling infeasible.
"""

from __future__ import annotations

from typing import Iterator

from tools.sources.base import RestSource, SourceError, SourceSpec

SPEC = SourceSpec(
    name="courtlistener",
    base_url="https://www.courtlistener.com/api/rest/v4",
    description="US case law, dockets, judges (Free Law Project)",
    answers=(
        "case-law opinion search (federal + state)",
        "federal docket and docket-entry metadata",
        "judge directory records",
        "citation lookup / validation",
    ),
    cannot_answer=(
        "PACER filing document bodies (billable, needs separate receipt)",
        "non-US jurisdictions",
        "bulk crawl at free-tier quotas (~125 req/day)",
        "legal advice or holdings guarantees — metadata + opinion text only",
    ),
    tier=2,
    min_interval_s=3.0,
    headers=(("Authorization", "Token {api_key}"),),
    terms_url="https://www.courtlistener.com/help/api/",
    key_env_var="CALLISTO_COURTLISTENER_TOKEN",
)

MAX_PAGE_SIZE = 20


class CourtListenerAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def _require_key(self) -> str:
        key = self.source.api_key()
        if not key:
            raise SourceError(
                "CourtListener requires a token; "
                "set CALLISTO_COURTLISTENER_TOKEN")
        return key

    def search(self, query: str, search_type: str = "o",
               page_size: int = MAX_PAGE_SIZE,
               order_by: str = "") -> dict:
        """One search page. search_type: o opinions, r dockets+docs,
        d dockets, p judges, oa oral arguments."""
        self._require_key()  # fail before fetching, never mid-request
        params = {
            "q": query,
            "type": search_type,
            "page_size": max(1, min(int(page_size), MAX_PAGE_SIZE)),
        }
        if order_by:
            params["order_by"] = order_by
        url = self.source.build_url("/search/", params)
        return self.source.get_json(url)[0]

    def paginate(self, first_page: dict, max_pages: int = 5) -> Iterator[dict]:
        """Yield result lists across cursor pages. Follows `next` verbatim."""
        page = first_page
        for _ in range(max_pages):
            yield page.get("results", [])
            nxt = page.get("next")
            if not nxt:
                return
            page = self.source.get_json(nxt)[0]

    def search_all(self, query: str, search_type: str = "o",
                   max_pages: int = 5) -> list[dict]:
        results: list[dict] = []
        for chunk in self.paginate(
                self.search(query, search_type=search_type),
                max_pages=max_pages):
            results.extend(chunk)
        return results

    def cluster(self, cluster_id: int) -> dict:
        self._require_key()
        url = self.source.build_url(f"/clusters/{int(cluster_id)}/")
        return self.source.get_json(url)[0]

    def opinion(self, opinion_id: int) -> dict:
        self._require_key()
        url = self.source.build_url(f"/opinions/{int(opinion_id)}/")
        return self.source.get_json(url)[0]

    def cite_lookup(self, citation: str) -> dict:
        self._require_key()
        url = self.source.build_url("/citation-lookup/", {"citation": citation})
        return self.source.get_json(url)[0]
