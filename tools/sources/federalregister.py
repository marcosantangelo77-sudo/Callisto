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

    # tuple, not a comma-joined string: build_url() urlencodes with
    # doseq=True so each field becomes its own repeated fields[] param.
    # The FR API 400s on a single comma-joined fields[] value (the defect
    # documented in the Task-61 health run and never fixed until now).
    # Also 2026-08: the API renamed 'published_at' -> 'publication_date'
    # ("field 'published_at' not valid", live-verified); keep our normalized
    # key name via _FIELD_RENAME below.
    FIELDS = ("title", "type", "abstract", "action", "publication_date",
              "effective_on", "docket_ids", "citation", "document_number",
              "html_url", "agencies")
    # API field name -> key we expose (old adapter contract preserved)
    _FIELD_RENAME = {"publication_date": "published_at"}

    def search(self, conditions: str = "", query_term: str = "",
               order: str = "newest", limit: int = 20,
               extra_params: dict | None = None) -> dict:
        """Term search. Both `conditions` and `query_term` feed the FR
        `conditions[term]` param (merged when both given) — the API 500s
        on a bare `conditions=` value (live-verified 2026-08-24)."""
        params: dict = {
            "per_page": min(int(limit), 1000),
            "order": order,
            "fields[]": self.FIELDS,
        }
        term = " ".join(t for t in (query_term.strip(), conditions.strip())
                        if t)
        if term:
            params["conditions[term]"] = term
        params.update(extra_params or {})
        url = self.source.build_url("/documents.json", params)
        return self._rename(self.source.get_json(url)[0])

    def document(self, document_number: str) -> dict:
        url = self.source.build_url(
            f"/documents/{document_number}.json",
            {"fields[]": self.FIELDS})
        return self._rename(self.source.get_json(url)[0])

    @classmethod
    def _rename(cls, payload: dict) -> dict:
        """Map renamed API fields back to the keys this adapter always
        exposed (publication_date -> published_at). The real response
        shapes are {'results': [...]} for /documents.json and a bare
        document dict for /documents/{id}.json — neither has ever had a
        'documents' key."""
        docs = payload.get("results")
        targets = docs if isinstance(docs, list) else [payload]
        for d in targets:
            if not isinstance(d, dict):
                continue
            for api_name, our_name in cls._FIELD_RENAME.items():
                if api_name in d:
                    d[our_name] = d.pop(api_name)
        return payload
