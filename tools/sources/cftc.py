"""CFTC Commitments of Traders — positioning of futures market cohorts.
Tier 1.

publicreporting.cftc.gov (Socrata SODA API) serves the weekly COT reports:
/resource/6dca-aqww (Legacy Futures-Only) and /resource/jhn-pzuq
(Disaggregated Futures-Only). No key; SODA allows anonymous ~few req/s —
we self-limit to 1 req/s and cap $limit at 1000. SoQL-style params apply
($where, $select, $order, $limit).

Key columns: report_date_as_yyyy_mm_dd, cftc_contract_market_code,
open_interest_all, noncomm_positions_long_all / _short_all,
comm_positions_long_all / _short_all (legacy report naming).

Answers: weekly positioning by trader cohort per contract (long/short/
spreading), open interest, week-over-week position changes since 2006
(legacy) / 2011+ (disaggregated).
Cannot answer: daily positioning (weekly report only), individual trader
identity (concentration ratios only), spot/futures basis beyond OI.
"""

from __future__ import annotations

import re

from tools.sources.base import RestSource, SourceError, SourceSpec

SPEC = SourceSpec(
    name="cftc_cot",
    base_url="https://publicreporting.cftc.gov/resource",
    description="CFTC Commitments of Traders: futures positioning by cohort",
    answers=(
        "weekly long/short positioning per futures contract",
        "commercial vs non-commercial cohort splits (legacy COT)",
        "producer/merchant/swap/money-manager splits (disaggregated)",
    ),
    cannot_answer=(
        "daily or intraday positioning (weekly publication)",
        "individual trader identities",
        "pre-2006 legacy-report history via this endpoint",
    ),
    tier=1,
    min_interval_s=1.0,
    terms_url="https://www.cftc.gov/PrivacyPolicy/index.htm",
)

LEGACY_FUTURES_ONLY = "6dca-aqww"
DISAGG_FUTURES_ONLY = "jhn-pzuq"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CftcCotAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def query(self, dataset: str, where: str = "",
              select: str = "", limit: int = 52, order: str = "") -> dict:
        """Socrata query on a COT resource. where example:
        "cftc_contract_market_code='088691'" (WTI)."""
        if dataset not in (LEGACY_FUTURES_ONLY, DISAGG_FUTURES_ONLY):
            raise ValueError(f"unknown COT dataset {dataset!r}")
        params: dict[str, str] = {"$limit": max(1, min(int(limit), 1000))}
        if where:
            params["$where"] = where
        if select:
            params["$select"] = select
        if order:
            params["$order"] = order
        url = self.source.build_url(f"/{dataset}.json", params)
        data, rec = self.source.get_json(url)
        return {"rows": data,
                "_fetch": {"url": rec.url, "sha256": rec.content_sha256,
                           "fetched_at": rec.fetched_at}}

    def contract_history(self, market_code: str, weeks: int = 52,
                         disaggregated: bool = False) -> dict:
        code = market_code.strip()
        if not re.fullmatch(r"[0-9A-Za-z]+", code):
            raise ValueError(f"bad contract market code {market_code!r}")
        ds = DISAGG_FUTURES_ONLY if disaggregated else LEGACY_FUTURES_ONLY
        where = f"cftc_contract_market_code='{code}'"
        return self.query(ds, where=where, limit=max(1, min(int(weeks), 1000)),
                          order="report_date_as_yyyy_mm_dd DESC")
