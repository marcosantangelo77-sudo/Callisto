"""Federal Register — rules, notices, proposed regulations. Tier 1 (documents
with structured metadata).

federalregister.gov/api/v1. No key. No stated hard rate limit; be polite.
We self-limit to ~2 req/s.

Answers: executive orders, agency rules and proposed rules with effective
dates, comment periods, docket references, full XML/text bodies.
Cannot answer: state law, case law (use CourtListener), congressional
statutes as enacted (the FR carries notices of enactment, not the code),
anything before 1994 (full-text coverage start; earlier in the print
archive only).
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="federalregister",
    base_url="https://www.federalregister.gov/api/v1",
    description="US Federal Register: rules, proposed rules, notices, EO",
    answers=(
        "executive orders and presidential documents",
        "final/proposed agency rules with dates and docket refs",
        "comment-period status and regulation docket pointers",
    ),
    cannot_answer=(
        "state law or court opinions",
        "US Code text as enacted",
        "documents before 1994 (print archive only)",
    ),
    tier=1,
    min_interval_s=0.5,
    terms_url="https://www.federalregister.gov/learn/about-the-federal-register",
)


class FederalRegisterAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    # NOTE: the FR API rejects comma-joined values in one fields[] param
    # (verified live: 'title,type' -> 400 "field not valid"); each field
    # must be its own repeated fields[] entry.
    FIELDS = ("title", "type", "abstract", "action", "publication_date",
              "effective_on", "docket_ids", "citation", "document_number",
              "html_url", "agencies")

    def search(self, conditions: str = "", query_term: str = "",
               order: str = "newest", limit: int = 20,
               extra_params: dict | None = None) -> dict:
        """`conditions` uses FR filter syntax, e.g. 'agencies[]=epa'."""
        params: dict = {
            "per_page": min(int(limit), 1000),
            "order": order,
        }
        for f in self.FIELDS:
            params.setdefault("fields[]", [])
            if isinstance(params["fields[]"], list):
                params["fields[]"].append(f)
        if conditions:
            params["conditions[term]"] = conditions
        if query_term:
            params["conditions[term]"] = query_term
        params.update(extra_params or {})
        url = self.source.build_url("/documents.json", params)
        data = self.source.get_json(url)[0]
        # The FR API returns `results` when fields[] is requested and
        # `documents` otherwise — normalise to `documents`.
        if "documents" not in data and isinstance(data.get("results"), list):
            data["documents"] = data.pop("results")
        return data

    def document(self, document_number: str) -> dict:
        url = self.source.build_url(
            f"/documents/{document_number}.json",
            {"fields[]": self.FIELDS})
        return self.source.get_json(url)[0]
