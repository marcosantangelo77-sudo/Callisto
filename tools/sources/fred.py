"""FRED — Federal Reserve Economic Data, ~800k macro series. Tier 1.

St. Louis Fed API (api.stlouisfed.org). Free key required
(research.stlouisfed.org/docs/api/api_key.html); set CALLISTO_FRED_API_KEY.
Rate limit: no published hard ceiling; we self-limit to ~2 req/s and ask
users to stay under 120 req/min per the docs' guidance.

Answers: macro time series observations (CPI, unemployment, GDP, rates),
series metadata/search.
Cannot answer: real-time intraday data, non-economic domains, firm-level
financials, revisions history beyond what FRED vintage pages carry.
"""

from __future__ import annotations

from typing import Any

from tools.sources.base import RestSource, SourceError, SourceSpec

SPEC = SourceSpec(
    name="fred",
    base_url="https://api.stlouisfed.org/fred",
    description="Federal Reserve Economic Data: macro time series",
    answers=(
        "macroeconomic time series (CPI, unemployment, GDP, rates, ...)",
        "series metadata and full-text series search",
    ),
    cannot_answer=(
        "intraday/real-time market data",
        "firm-level financials (use EDGAR)",
        "forecasts or model priors — observations only",
    ),
    tier=1,
    min_interval_s=0.5,
    terms_url="https://research.stlouisfed.org/docs/api/terms_of_use.html",
    key_env_var="CALLISTO_FRED_API_KEY",
)


class FredAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def _require_key(self) -> str:
        key = self.source.api_key()
        if not key:
            raise SourceError(
                "FRED requires an API key; set CALLISTO_FRED_API_KEY")
        return key

    def _url(self, path: str, params: dict) -> str:
        params = dict(params)
        params["api_key"] = self._require_key()
        params["file_type"] = "json"
        return self.source.build_url(path, params)

    def series_observations(self, series_id: str,
                            start: str = "", end: str = "",
                            limit: int = 0) -> dict:
        """Observations for one series, oldest-first. Returns dict with the
        fetch record attached under '_fetch'."""
        params = {"series_id": series_id.upper()}
        if start:
            params["observation_start"] = start
        if end:
            params["observation_end"] = end
        if limit:
            params["limit"] = int(limit)
        url = self._url("/series/observations", params)
        data, rec = self.source.get_json(url)
        data["_fetch"] = {"url": rec.url, "sha256": rec.content_sha256,
                          "fetched_at": rec.fetched_at}
        return data

    def series_search(self, query: str, limit: int = 10) -> dict:
        """Full-text series search."""
        url = self._url("/series/search",
                        {"search_text": query, "limit": int(limit)})
        return self.source.get_json(url)[0]

    def series_info(self, series_id: str) -> dict:
        url = self._url("/series", {"series_id": series_id.upper()})
        return self.source.get_json(url)[0]
