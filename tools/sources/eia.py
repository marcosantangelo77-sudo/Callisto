"""EIA open data — energy production, prices, inventories. Tier 1.

api.eia.gov/v2 — the v2 API replaced the old v1 series endpoints.
Key REQUIRED (free, api.eia.gov/register); header `X-Api-Key`; set
CALLISTO_EIA_API_KEY. Stated limit: unlimited within fair use, but keep
under ~5 req/s; we self-limit to 2 req/s.

Route shape: /v2/{route...}/data?api_key=..&frequency=monthly
&data[0]=value&facets[duoarea][]=NUS&sort[0][column]=period
Route is a full facet hierarchy like 'petroleum/stoc/wstk' (weekly
petroleum stocks) or 'natural-gas/stor' (weekly gas storage). The legacy
single-series shortcut GET /v2/seriesid/{SERIES_ID} was REMOVED by EIA:
it now answers 404 for every id (verified live 2026-08-24 on COPRPUS.A).
series() therefore translates classic petroleum 'PET.<ID>.<FREQ>' ids
onto their facet route (/v2/petroleum/stoc/... exposes e.g. WCSSTUS1 via
the series facet); any other id must go through browse() with an
explicit route discovered from GET /v2/<route>.

Answers: crude/petroleum prices (WTI, Brent, RBOB), petroleum product
supplied, natural gas storage inventories, electricity generation by
source, retail gasoline/diesel prices.
Cannot answer: intraday spot ticks (weekly/monthly official series),
non-public proprietary surveys, forecasts beyond STEO metadata.
"""

from __future__ import annotations

import re

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
        """Single official series. The legacy /v2/seriesid shortcut 404s
        for every id since EIA's v2 reorganisation, so classic petroleum
        ids of the form 'PET.<ID>.<FREQ>' are resolved onto the facet
        route /v2/petroleum/.../data with a series facet filter; other
        ids raise with guidance to use browse() and an explicit route.
        start/end are period strings matching the frequency (YYYY-MM-DD,
        YYYY-MM, or YYYY)."""
        self._require_key()
        sid = series_id.upper()
        resolved = _series_route(sid)
        if resolved is None:
            raise SourceError(
                f"EIA v2 removed /seriesid/{sid}; no automatic v2 route "
                "is known for this id — call browse(route, facets=...) "
                "with the hierarchy from GET /v2/<route> discovery")
        route_path, facets = resolved
        params: dict[str, str] = {
            "frequency": frequency,
            "data[0]": "value",
            **_facet_params(facets),
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if length:
            params["length"] = int(length)
        url = self.source.build_url(f"/{route_path.strip('/')}/data", params)
        data, rec = self.source.get_json(url)
        resp = data.get("response") or {}
        rows = resp.get("data") or []
        # keep only the requested series when the route mixes several
        want = resolved[1].get("series", "")
        if want and all("series" in row for row in rows):
            rows = [row for row in rows if row.get("series") == want]
            resp["data"] = rows
        out = {"series_id": sid,
               "data": resp.get("data", []),
               "_fetch": {"url": rec.url, "sha256": rec.content_sha256,
                          "fetched_at": rec.fetched_at}}
        return out

    def browse(self, route: str, facets: dict | None = None,
               data_cols: tuple[str, ...] = ("value",),
               frequency: str = "monthly") -> dict:
        """Walk the facet hierarchy, e.g.
        browse('petroleum/stoc/wstk', facets={'duoarea': 'NUS'})."""
        self._require_key()
        params: dict[str, str] = {"frequency": frequency}
        for i, col in enumerate(data_cols):
            params[f"data[{i}]"] = col
        params.update(_facet_params(facets or {}))
        url = self.source.build_url(f"/{route.strip('/')}/data", params)
        data, rec = self.source.get_json(url)
        resp = data.get("response") or {}
        resp["_fetch"] = {"url": rec.url, "sha256": rec.content_sha256,
                          "fetched_at": rec.fetched_at}
        return resp


# ── v2 route resolution ───────────────────────────────────────────────────
# The legacy /v2/seriesid/{ID} endpoint was removed (live-verified 404 on
# 2026-08-24). Known classic ids map to their v2 facet route here; anything
# else must go through browse() with an explicit route discovered via the
# route endpoints (GET /v2/ returns the top-level routes).

def _facet_params(facets: dict) -> dict[str, str]:
    """{'duoarea': 'NUS'} -> {'facets[duoarea][]': 'NUS'} (one value per
    facet keeps URLs simple; multi-value facets can be added as needed)."""
    out: dict[str, str] = {}
    for k, vals in facets.items():
        items = vals if isinstance(vals, (list, tuple)) else [vals]
        if items:
            out[f"facets[{k}][]"] = items[0]
    return out


# classic petroleum series-id prefix -> (v2 route, fixed facets).
# 'PET.WCSSTUS1.W' style ids live under /v2/petroleum/stoc/wstk (weekly)
# or /v2/petroleum/stoc/ts (monthly+); the series facet isolates the id.
_SERIES_ROUTES: dict[str, tuple[str, dict]] = {
    "W": ("/petroleum/stoc/wstk", {}),
    "M": ("/petroleum/stoc/ts", {}),
    "A": ("/petroleum/stoc/ts", {}),
}


def _series_route(series_id: str):
    """Map a classic 'PET.<ID>.<FREQ>' id to its v2 (route, facets), or
    None when no automatic mapping is known."""
    m = re.fullmatch(r"PET\.([A-Z0-9]+)\.([WMA])", series_id)
    if not m:
        return None
    sid, freq = m.group(1), m.group(2)
    route, extra = _SERIES_ROUTES[freq]
    return (route, {"series": sid, **extra})
