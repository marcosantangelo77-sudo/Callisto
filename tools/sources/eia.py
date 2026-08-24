"""EIA open data — energy production, prices, inventories. Tier 1.

api.eia.gov/v2 — the v2 API replaced the old v1 series endpoints.
Key REQUIRED (free, api.eia.gov/register); header `X-Api-Key`; set
CALLISTO_EIA_API_KEY. Stated limit: unlimited within fair use, but keep
under ~5 req/s; we self-limit to 2 req/s.

Route shape: /v2/{route...}/data?api_key=..&frequency=monthly
&data[0]=value&facets[{facet}][]={VALUE}&start=&end=&sort[0][column]=period
The legacy v1-style shortcut GET /v2/seriesid/{ID} was RETIRED (HTTP 404)
in the 2024-25 v2 reorganisation — series ids are now facet values on a
data route (e.g. facets[series][]=RWTC on petroleum/pri/spt, or
facets[seriesId][]=COPRPUS on steo). That is what series() wraps via the
route registry below; browse() walks any route hierarchy directly
(e.g. 'petroleum/pri/spt', 'natural-gas/stor/sum').

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


#: Known series id -> (v2 data route, facet name) mapping. The retired
#: /v2/seriesid shortcut means an id alone no longer locates a series;
#: each id lives as a facet value under one specific route.
_SERIES_ROUTES: dict[str, tuple[str, str]] = {
    # WTI / Brent / diesel spot prices live in the petroleum spot route,
    # keyed by the `series` facet.
    "RWTC": ("petroleum/pri/spt", "series"),
    "RBRTE": ("petroleum/pri/spt", "series"),
    "EER_EPD2DXL0_PF4_Y35NY_DPG": ("petroleum/pri/spt", "series"),
    # STEO projection series are keyed by `seriesId` with NO frequency
    # suffix — 'COPRPUS.A'/'COPRPUS.M' were v1-era spellings.
    "COPRPUS": ("steo/data", "seriesId"),
}


class EiaAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def _require_key(self) -> str:
        key = self.source.api_key()
        if not key:
            raise SourceError("EIA requires an API key; set CALLISTO_EIA_API_KEY")
        return key

    @staticmethod
    def _normalize_series_id(series_id: str) -> str:
        """Strip legacy v1 frequency suffixes ('COPRPUS.A' -> 'COPRPUS').
        In v1 the trailing .A/.M/.W selected annual/monthly/weekly cadence;
        in v2 cadence is the ?frequency= parameter and the bare id is the
        facet value."""
        base = series_id.strip().upper()
        if "." in base:
            stem, suffix = base.rsplit(".", 1)
            if len(suffix) == 1 and stem:
                return stem
        return base

    def _facet_params(self, values: list[str]) -> dict[str, str]:
        params: dict[str, str] = {}
        for j, v in enumerate(values):
            params[f"facets[value][{j}]"] = v
        return params

    def _get(self, route: str, params: dict[str, str],
             series_id: str) -> dict:
        url = self.source.build_url(f"/{route.strip('/')}/data"
                                    if not route.endswith("/data")
                                    else f"/{route.strip('/')}", params)
        data, rec = self.source.get_json(url)
        resp = data.get("response", data)
        out = {"series_id": series_id.upper(),
               "data": resp.get("data", []),
               "_fetch": {"url": rec.url, "sha256": rec.content_sha256,
                          "fetched_at": rec.fetched_at}}
        if not out["data"]:
            raise SourceError(
                f"EIA returned ZERO rows for series {series_id} "
                f"(route {route}) — check the series/route mapping")
        return out

    def series(self, series_id: str, frequency: str = "monthly",
               start: str = "", end: str = "", length: int = 0) -> dict:
        """Single official series. start/end are period strings matching
        the frequency (YYYY-MM-DD, YYYY-MM, or YYYY)."""
        self._require_key()
        sid = self._normalize_series_id(series_id)
        route, facet = _SERIES_ROUTES.get(sid, ("steo/data", "seriesId"))
        params: dict[str, str] = {"frequency": frequency,
                                  f"facets[{facet}][]": sid}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if length:
            params["length"] = int(length)
        return self._get(route, params, series_id)

    def browse(self, route: str, facets: dict | None = None,
               data_cols: tuple[str, ...] = ("value",),
               frequency: str = "monthly") -> dict:
        """Walk the facet hierarchy, e.g.
        browse('petroleum/pri/spt', facets={'series': 'RWTC'})."""
        self._require_key()
        path = route.strip("/")
        params: dict[str, str] = {"frequency": frequency}
        for i, col in enumerate(data_cols):
            params[f"data[{i}]"] = col
        for k, vals in (facets or {}).items():
            items = vals if isinstance(vals, (list, tuple)) else [vals]
            for j, v in enumerate(items[:1]):  # one value keeps URL simple
                params[f"facets[{k}][]"] = v
        url = self.source.build_url(f"/{path}/data", params)
        data, rec = self.source.get_json(url)
        resp = data.get("response", data)
        resp["_fetch"] = {"url": rec.url, "sha256": rec.content_sha256,
                          "fetched_at": rec.fetched_at}
        return resp
