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
                            limit: int = 0,
                            sort_order: str = "") -> dict:
        """Observations for one series, oldest-first. Returns dict with the
        fetch record attached under '_fetch'.

        The series TITLE (e.g. 'Unemployment Rate') is attached under
        '_series_title'. Without it the observations body carries only
        dates and numeric values — no word of the question's topic — so
        the relevance gate scored FRED 0% on exactly the questions it is
        the best source for (live e2e run 2026-08-24, break log B2b).

        TRUNCATION IS VISIBLE (defect from the same live run): when the
        API hits `limit` inside the requested window, '_truncated' is set
        to a dict naming the limit and the first/last observation dates.
        A body cut short by the transport must never read as 'the series
        ends here'. The planner passes sort_order='desc' so the retained
        observations are the MOST RECENT ones."""
        params = {"series_id": series_id.upper()}
        if start:
            params["observation_start"] = start
        if end:
            params["observation_end"] = end
        if limit:
            params["limit"] = int(limit)
        if sort_order:
            params["sort_order"] = sort_order
        url = self._url("/series/observations", params)
        data, rec = self.source.get_json(url)
        try:
            obs = data.get("observations") or []
            if limit and len(obs) >= int(limit):
                # count_end=1 keeps the live edge visible even when the
                # limit lands exactly; FRED also returns it, but we do not
                # depend on the remote contract for an honesty flag.
                data["_truncated"] = {
                    "limit": int(limit),
                    "n_observations": len(obs),
                    "first": obs[0].get("date", ""),
                    "last": obs[-1].get("date", ""),
                }
        except Exception:  # noqa: BLE001 — flagging must not fail the fetch
            pass
        try:
            info_url = self._url("/series", {"series_id": series_id.upper()})
            info, _ = self.source.get_json(info_url)
            seriess = info.get("seriess") or []
            if seriess and seriess[0].get("title"):
                data["_series_title"] = seriess[0]["title"]
        except Exception:  # noqa: BLE001 — title is enrichment; a metadata
            pass          # failure must not fail the observation fetch
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
