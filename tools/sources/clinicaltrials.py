"""ClinicalTrials.gov API v2 — trial registrations and outcomes. Tier 1.

clinicaltrials.gov/api/v2. No key. Stated guidance: no more than
10 req/s; we self-limit to ~2 req/s.

Answers: trial registry entries (design, eligibility, arms, endpoints),
recruitment status, posted results and adverse events for completed
trials.
Cannot answer: unpublished results, FDA approval decisions, individual
participant data, trials never registered, real-time status (registry
updates lag site updates).
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="clinicaltrials",
    base_url="https://clinicaltrials.gov/api/v2",
    description="ClinicalTrials.gov v2: trial registrations and outcomes",
    answers=(
        "trial design/arms/endpoints by NCT id or search",
        "recruitment status and enrollment counts",
        "posted results for completed trials (incl. adverse events)",
    ),
    cannot_answer=(
        "unpublished results and FDA decisions",
        "individual participant data",
        "unregistered trials",
        "real-time site status",
    ),
    tier=1,
    min_interval_s=0.5,
    terms_url="https://clinicaltrials.gov/about-site/terms-conditions",
)


class ClinicalTrialsAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def get_study(self, nct_id: str) -> dict:
        nct = nct_id.strip().upper()
        if not nct.startswith("NCT"):
            raise ValueError(f"not an NCT id: {nct_id!r}")
        url = self.source.build_url(f"/studies/{nct}")
        return self.source.get_json(url)[0]

    def search_studies(self, query_term: str = "", condition: str = "",
                       intervention: str = "", status: str = "",
                       limit: int = 20) -> dict:
        params: dict = {"pageSize": min(int(limit), 100)}
        if query_term:
            params["query.term"] = query_term
        if condition:
            params["query.cond"] = condition
        if intervention:
            params["query.intr"] = intervention
        if status:
            params["filter.overallStatus"] = status
        url = self.source.build_url("/studies", params)
        return self.source.get_json(url)[0]
