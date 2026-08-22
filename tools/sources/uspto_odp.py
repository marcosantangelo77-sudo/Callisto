"""USPTO Open Data Portal — patent applications, the stable successor to
PatentsView's Search API. Tier 1.

PatentsView's original PatentsView API was retired; its data moved to the
USPTO Open Data Portal (ODP), api.uspto.gov/api/v1 (verified 2026 against
data.uspto.gov/support/transition-guide/patentsview). Key REQUIRED: header
`X-API-KEY`; obtain at data.uspto.gov/getting-started; set
CALLISTO_USPTO_ODP_KEY. Data refreshed daily; we self-limit to 1 req/s.

Search syntax: simple query string over 100+ documented fields, e.g.
  applicationMetaData.applicationTypeLabelName:Utility
  applicationMetaData.applicationStatusDescriptionText:"Patented Case"
GET form: /patent/applications/search?q=<query>&offset=&limit=
POST form accepts the same query in a JSON body with a pagination object;
we use POST for long queries because URL length limits are real there.

Answers: patent/application bibliographic search by title, assignee,
inventor, CPC class, status; application detail by application number;
continuity and term-adjustment lookups.
Cannot answer: full patent grant documents/PDF text (bibliographic +
status data only), litigation outcomes, trademarks here (separate ODP
surface), non-US patents.
"""

from __future__ import annotations

from typing import Any

from tools.sources.base import RestSource, SourceError, SourceSpec

SPEC = SourceSpec(
    name="uspto_odp",
    base_url="https://api.uspto.gov/api/v1",
    description="USPTO Open Data Portal: patent applications, status, continuity",
    answers=(
        "patent application bibliographic search (assignee, inventor, title, CPC)",
        "application status and prosecution history metadata",
        "continuity and patent-term-adjustment lookups",
    ),
    cannot_answer=(
        "full grant document text / PDFs (bibliographic + status only)",
        "patent litigation outcomes",
        "trademarks (separate surface)",
        "non-US patent offices",
    ),
    tier=1,
    min_interval_s=1.0,
    headers=(("X-API-KEY", "{api_key}"),),
    terms_url="https://developer.uspto.gov/terms",
    key_env_var="CALLISTO_USPTO_ODP_KEY",
)

MAX_PAGE = 100  # ODP caps limit well above our default; stay conservative


class UsptoOdpAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def _require_key(self) -> str:
        key = self.source.api_key()
        if not key:
            raise SourceError(
                "USPTO ODP requires an API key; set CALLISTO_USPTO_ODP_KEY")
        return key

    def search_applications(self, query: str, offset: int = 0,
                            limit: int = 25) -> dict:
        """GET /patent/applications/search. Query uses the ODP simplified
        syntax over documented fields."""
        self._require_key()  # fail before fetching, never mid-request
        url = self.source.build_url(
            "/patent/applications/search",
            {"q": query, "offset": int(offset),
             "limit": max(1, min(int(limit), MAX_PAGE))})
        return self.source.get_json(url)[0]

    def search_applications_post(self, query: str, offset: int = 0,
                                 limit: int = 25) -> dict:
        """POST form — same query language in a JSON body (long queries)."""
        self._require_key()
        url = self.source.build_url("/patent/applications/search")
        payload: dict[str, Any] = {
            "q": query,
            "pagination": {"offset": str(int(offset)),
                           "limit": str(max(1, min(int(limit), MAX_PAGE)))},
        }
        return self.source.post_json(url, payload)[0]

    def application(self, application_number: str) -> dict:
        num = str(application_number).strip().replace(",", "")
        if not num.isdigit():
            raise ValueError(f"not a US application number: {application_number!r}")
        url = self.source.build_url(f"/patent/applications/{num}")
        return self.source.get_json(url)[0]
