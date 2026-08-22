"""Treasury Fiscal Data — rates, auctions, debt. Tier 1.

api.fiscaldata.treasury.gov. No key. Stated limit: 1 req/s sustained; we
self-limit to exactly that.

Answers: daily treasury yield-curve rates, debt to the penny, auction
results, TGA balances, interest expense.
Cannot answer: equity prices, corporate bond curves, real yields
(published series are nominal except TIPS-related series), intraday data.
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="treasury",
    base_url="https://api.fiscaldata.treasury.gov/services/api/fiscal_service",
    description="US Treasury Fiscal Data: rates, auctions, federal debt",
    answers=(
        "daily par yield-curve rates (avg of business days by month too)",
        "debt to the penny / debt outstanding by instrument",
        "Treasury auction results and interest expense",
    ),
    cannot_answer=(
        "equity prices or corporate bond curves",
        "intraday data (daily/monthly granularity)",
        "state/municipal debt",
    ),
    tier=1,
    min_interval_s=1.0,
    terms_url="https://fiscaldata.treasury.gov/api-documentation/#terms-of-service",
)

# Well-known datasets from the public catalog (fiscaldata.treasury.gov/
# datasets). Use query(dataset, ...) directly for anything else in the
# catalog rather than adding per-dataset wrappers.
DATASET_AVG_INTEREST_RATES = "v2/accounting/od/avg_interest_rates"
DATASET_DEBT_TO_PENNY = "v2/debt/mspd/mspd_table_1"


class TreasuryAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def query(self, dataset: str, filters: str = "", fields: str = "",
              sort: str = "-record_date", limit: int = 100) -> dict:
        """Generic Fiscal Data query language:
        filter=record_date:gte:2024-01-01&fields=...&sort=..."""
        params: dict = {"limit": min(int(limit), 10000), "sort": sort}
        if filters:
            params["filter"] = filters
        if fields:
            params["fields"] = fields
        url = self.source.build_url(f"/{dataset.strip('/')}", params)
        return self.source.get_json(url)[0]
