"""BLS Public Data API v2.0 — employment, CPI, payrolls. Tier 1.

api.bls.gov/public/api/v2/timeseries/data (POST JSON).
No key: 25 queries/day, 25 series/request, 3-year history.
With key (CALLISTO_BLS_API_KEY): 500/day, 50 series, 20-year history.
We self-limit to ~1 req/s and enforce the caps in code so a request is
never silently truncated by the server.

Answers: employment situation (CES/CPS), CPI and components, payrolls,
unemployment rates, PPI — any published BLS series id.
Cannot answer: forecasts/nowcasts, microdata, not-yet-published periods;
the no-key tier is capped at 25 requests/day and 3-year history.
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceError, SourceSpec

SPEC = SourceSpec(
    name="bls",
    base_url="https://api.bls.gov/public/api/v2",
    description="BLS time series: employment, CPI, payrolls, prices",
    answers=(
        "employment/payrolls/unemployment series",
        "CPI/PPI price index levels and derived inflation",
        "any published BLS series by series id",
    ),
    cannot_answer=(
        "forecasts or nowcasts",
        "individual microdata",
        "series not yet published for the current period",
        "no-key tier: 25 requests/day, 25 series/call, 3-year history",
    ),
    tier=1,
    min_interval_s=1.0,
    terms_url="https://www.bls.gov/developers/",
)

MAX_SERIES_NO_KEY = 25
MAX_SERIES_WITH_KEY = 50
YEARS_NO_KEY = 3
YEARS_WITH_KEY = 20


class BlsAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    @property
    def api_key(self) -> str:
        return self.source.api_key()

    def timeseries(self, series_ids: list[str], start_year: int,
                   end_year: int) -> dict:
        ids = [s.upper() for s in series_ids]
        keyed = bool(self.api_key)
        cap = MAX_SERIES_WITH_KEY if keyed else MAX_SERIES_NO_KEY
        max_years = YEARS_WITH_KEY if keyed else YEARS_NO_KEY
        if len(ids) > cap:
            raise SourceError(
                f"BLS {'keyed' if keyed else 'no-key'} tier caps requests at "
                f"{cap} series per call; got {len(ids)}")
        if int(end_year) - int(start_year) > max_years - 1:
            raise SourceError(
                f"BLS {'keyed' if keyed else 'no-key'} tier allows "
                f"{max_years}-year history per call")
        payload = {"seriesid": ids,
                   "startyear": str(int(start_year)),
                   "endyear": str(int(end_year))}
        url = self.source.build_url("/timeseries/data")
        if keyed:
            url += "&registrationkey=" + self.api_key
        data, rec = self.source.post_json(url, payload)
        data["_fetch"] = {"url": rec.url, "sha256": rec.content_sha256,
                          "fetched_at": rec.fetched_at}
        return data
