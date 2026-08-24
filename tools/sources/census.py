"""Census Data API — construction, housing, retail, demographics. Tier 1.

api.census.gov/data/{year}/{dataset}?get=VARS&for=GEO:*[&in=...].
Key REQUIRED as of 2026-08 (was 'optional for light use'): any data query
without a valid key gets HTTP 302 -> data/missing_key.html, which parses
as non-JSON (live-verified across acs1/acs5/eits). Set
CALLISTO_CENSUS_API_KEY (api.census.gov/data/key_signup.html).
No stated per-second ceiling; we self-limit to ~2 req/s. Responses are
a flat JSON array: first row = column names, subsequent rows = values
(all strings; 'N'/'-' etc. mean suppressed — preserved verbatim).

Answers: housing starts/completions/sales surveys, new residential
construction, retail e-commerce & monthly retail trade, ACS housing and
population tables by geography.
Cannot answer: real-time or intraday data, sub-monthly geography detail
on some surveys, business identifiers/firm financials, suppressed cells
(they come back as suppression codes, never imputed).
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="census",
    base_url="https://api.census.gov/data",
    description="Census: housing starts, retail trade, ACS demographics",
    answers=(
        "new residential construction (starts, permits, completions)",
        "monthly/annual retail trade and e-commerce sales",
        "ACS housing/population tables by state/county/place",
    ),
    cannot_answer=(
        "real-time data (surveys lag weeks to months)",
        "suppressed small-area values (returned as codes, not imputed)",
        "firm-level financials",
    ),
    tier=1,
    min_interval_s=0.5,
    terms_url="https://www.census.gov/data/developers/about.html",
    key_env_var="CALLISTO_CENSUS_API_KEY",
)


class CensusAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def query(self, year: str, dataset: str, get_vars: list[str],
              geo_for: str, geo_in: str = "", predicate: str = "") -> dict:
        """Flat-array query. dataset like 'acs/acs1', 'timeseries/eits/resconst'.
        Returns {'columns': [...], 'rows': [[...], ...], '_fetch': {...}}."""
        params: dict[str, str] = {
            "get": ",".join(get_vars),
            "for": geo_for,
        }
        if geo_in:
            params["in"] = geo_in
        if predicate:
            # predicates use ':' time filters on timeseries datasets;
            # urllib encodes it fine inside the value
            params[predicate.split("=", 1)[0]] = predicate.split("=", 1)[1]
        key = self.source.api_key()
        if key:
            params["key"] = key
        url = self.source.build_url(f"/{year}/{dataset}", params)
        data, rec = self.source.get_json(url)
        if not isinstance(data, list) or not data:
            raise ValueError(f"unexpected census response shape from {url}")
        out = {"columns": data[0], "rows": data[1:]}
        out["_fetch"] = {"url": rec.url, "sha256": rec.content_sha256,
                         "fetched_at": rec.fetched_at}
        return out

    def timeseries(self, dataset: str, get_vars: list[str], geo_for: str,
                   start: str = "", end: str = "") -> dict:
        """EITS-style timeseries with '&time=from+to' filtering."""
        pred = ""
        if start or end:
            pred = f"time={start}+{end}" if end else f"time={start}"
        return self.query("timeseries", dataset, get_vars, geo_for,
                          predicate=pred)
