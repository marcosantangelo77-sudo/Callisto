"""EIA open data — energy production, prices, inventories. Tier 1.

api.eia.gov/v2 — the v2 API replaced the old v1 series endpoints.
Key REQUIRED (free, api.eia.gov/register); header `X-Api-Key`; set
CALLISTO_EIA_API_KEY. Stated limit: unlimited within fair use, but keep
under ~5 req/s; we self-limit to 2 req/s.

Route shape: /v2/{route...}/data?api_key=..&frequency=monthly
&data[0]=value&facets[series][]=SERIES_ID&start=&end=&sort[]=period
Route is a path like 'seriesid/PET.WCESTUS1.W' (shortcut) or the full
hierarchy 'petroleum/stve/state'. The classic single-series shortcut
GET /v2/seriesid/{SERIES_ID}?frequency=... returns
{response:{data:[{period, value, ...}]}} — that is what series() wraps.

Answers: crude/petroleum prices (WTI, Brent, RBOB), petroleum product
supplied, natural gas storage inventories, electricity generation by
source, retail gasoline/diesel prices.
Cannot answer: intraday spot ticks (weekly/monthly official series),
non-public proprietary surveys, forecasts beyond STEO metadata.
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceError, SourceSpec

SPEC = SourceSpec(
    name="eia",
    base_url="https://api.eia.gov/v2",
    description="EIA: energy prices, production, inventories",
    answers=(
        "energy commodity prices (crude, products, natural gas)",
        "petroleum/natural gas inventories and production",
        "electricity generation and consumption by source",
    ),
    cannot_answer=(
        "intraday/real-time market ticks",
        "futures curves (exchange data, not EIA)",
        "sub-weekly granularity on most series",
    ),
    tier=1,
    min_interval_s=0.5,
    headers=(("X-Api-Key", "{api_key}"),),
    terms_url="https://www.eia.gov/about/copyrights_reuse.php",
    key_env_var="CALLISTO_EIA_API_KEY",
)


class EiaAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def _require_key(self) -> str:
        key = self.source.api_key()
        if not key:
            raise SourceError("EIA requires an API key; set CALLISTO_EIA_API_KEY")
        return key

    def series(self, series_id: str, frequency: str = "monthly",
               start: str = "", end: str = "", length: int = 0) -> dict:
        """Single official series via the /v2/seriesid shortcut.
        start/end are period strings matching the frequency (YYYY-MM-DD,
        YYYY-MM, or YYYY)."""
        self._require_key()
        path = f"/seriesid/{series_id.upper()}"
        params = {"frequency": frequency}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if length:
            params["length"] = int(length)
        url = self.source.build_url(path, params)
        data, rec = self.source.get_json(url)
        resp = data.get("response", data)
        out = {"series_id": series_id.upper(),
               "data": resp.get("data", []),
               "_fetch": {"url": rec.url, "sha256": rec.content_sha256,
                          "fetched_at": rec.fetched_at}}
        return out

    def browse(self, route: str, facets: dict | None = None,
               data_cols: tuple[str, ...] = ("value",),
               frequency: str = "monthly") -> dict:
        """Walk the facet hierarchy, e.g.
        browse('petroleum/stve/state', facets={'duoarea':'NUS'})."""
        self._require_key()
        params: dict[str, str] = {"frequency": frequency}
        for i, col in enumerate(data_cols):
            params[f"data[{i}]"] = col
        for k, vals in (facets or {}).items():
            items = vals if isinstance(vals, (list, tuple)) else [vals]
            for j, v in enumerate(items):
                params[f"facets[{k}][]"] = v
                break  # one value keeps URL simple; multi-facet via repeated keys
        url = self.source.build_url(f"/{route.strip('/')}/data", params)
        data, rec = self.source.get_json(url)
        resp = data.get("response", data)
        resp["_fetch"] = {"url": rec.url, "sha256": rec.content_sha256,
                          "fetched_at": rec.fetched_at}
        return resp
