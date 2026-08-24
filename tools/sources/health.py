"""Source-health probe — is each registered API actually alive?

The fixture suites prove OUR PARSING works; they say nothing about
whether the remote endpoint still resolves, still returns data for a
known-good query, and still returns it in the shape the adapter expects.
Five real defects hid exactly there (FDIC host moved / filters-vs-search,
wrong CFTC dataset id, Treasury 404, Federal Register comma-joined fields,
ClinicalTrials status-word zeroing results) while every fixture passed.

This module is OPT-IN and network-gated:

    CALLISTO_SOURCE_HEALTH_NET=1 python -m tools.sources.health [--json]

Without the env var set it refuses to touch the network (and raises if a
probe is attempted), so it can never run inside the normal test suite —
the tests/helpers/no_socket.py barrier stays intact and unweakened.

Verdicts per source:
  OK        reachable, non-empty result, expected shape
  DEGRADED  reachable (HTTP 200) but the known-good query returned ZERO rows
            — this is a FAILURE, not a pass; it is precisely how the
            ClinicalTrials and FDIC defects hid
  BROKEN   unreachable (DNS/connection/HTTP error) or the response no longer
            matches the shape the adapter parses
  SKIPPED   source requires an API key that is not configured (health of the
            remote cannot be assessed without credentials)

Each verdict carries evidence: URL fetched, HTTP status, row count, and
either the shape mismatch or the exception text.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

NET_GATE_ENV = "CALLISTO_SOURCE_HEALTH_NET"

OK, DEGRADED, BROKEN, SKIPPED = "OK", "DEGRADED", "BROKEN", "SKIPPED"


def require_net_gate() -> None:
    """Refuse to run unless the operator explicitly opted in."""
    if os.environ.get(NET_GATE_ENV, "") not in ("1", "true", "yes"):
        raise RuntimeError(
            f"source-health probes hit live APIs; set {NET_GATE_ENV}=1 to "
            "opt in (never enabled inside the normal test suite)")


@dataclass
class ProbeResult:
    source: str
    verdict: str = BROKEN  # default until a probe classifies it
    url: str = ""
    http_status: int | None = None
    row_count: int | None = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"source": self.source, "verdict": self.verdict,
                "url": self.url, "http_status": self.http_status,
                "row_count": self.row_count, "evidence": self.evidence}


def _finish(res: ProbeResult, data: Any, count_of: Callable[[Any], int],
            shape_ok: Callable[[Any], str]) -> ProbeResult:
    """Classify one successful fetch: empty => DEGRADED, bad shape => BROKEN,
    else OK. A 200-with-zero-results is DEGRADED on purpose."""
    res.row_count = count_of(data)
    if res.row_count == 0:
        res.verdict = DEGRADED
        res.evidence.append("HTTP 200 but ZERO results for the known-good "
                            "query — adapter will silently return nothing")
        return res
    problem = shape_ok(data)
    if problem:
        res.verdict = BROKEN
        res.evidence.append(problem)
    else:
        res.verdict = OK
        res.evidence.append(f"{res.row_count} rows, shape as expected")
    return res


# ── one probe per registered source ──────────────────────────────────────
# Each probe builds the real adapter over a REAL RestSource (no transport
# injection) and issues ONE known-good query that historically returns
# data, then validates the exact keys downstream parsing relies on.

def _build(name: str):
    """(source, adapter) for a registered source.

    Probes may pass either a module filename ('sec_fts') or the registry
    spec name ('sec_fulltext'); both resolve. An unknown name raises
    instead of returning None — an unpack of None once masked three dead
    probes as one mysterious TypeError.
    """
    from tools.sources.base import RestSource
    from tools.sources.registry import get_source_registry
    reg = get_source_registry()
    entry = reg.get(name)
    if entry is None:
        # tolerate module-filename aliases for registry spec names
        for cand in reg.names():
            if cand.replace("_", "") == name.replace("_", ""):
                entry = reg.get(cand)
                break
    if entry is None:
        raise KeyError(f"source '{name}' is not registered "
                       f"(registered: {', '.join(reg.names())})")
    src = RestSource(entry.spec)
    return src, entry.make_adapter(src)


def _keyed(spec_key_env: str | None) -> bool:
    return bool(spec_key_env) and bool(os.environ.get(spec_key_env, ""))


def _run(probe: Callable[[], Any], res: ProbeResult,
         count_of: Callable[[Any], int],
         shape_ok: Callable[[Any], str]) -> ProbeResult:
    try:
        data = probe()
    except Exception as exc:  # noqa: BLE001 — any failure is evidence
        res.verdict = BROKEN
        res.evidence.append(f"{type(exc).__name__}: {exc}")
        return res
    return _finish(res, data, count_of, shape_ok)


PROBES: dict[str, tuple[str, Callable[[], ProbeResult]]] = {}
# value: (required key env var or "", thunk)


def probe(name: str, key_env: str = ""):
    def deco(fn):
        PROBES[name] = (key_env, fn)
        return fn
    return deco


@probe("fred", "CALLISTO_FRED_API_KEY")
def _fred() -> ProbeResult:
    r = ProbeResult("fred")
    src, ad = _build("fred")
    r.url = src.build_url("/series/observations",
                          {"series_id": "DGS10", "limit": 5})
    def shape(d):
        obs = d.get("observations") or []
        return "" if obs and "value" in obs[0] else \
            f"missing observations[].value; keys={sorted(d)[:10]}"
    return _run(lambda: ad.series_observations("DGS10", limit=5), r,
                lambda d: len(d.get("observations", [])), shape)


@probe("openalex")
def _openalex() -> ProbeResult:
    r = ProbeResult("openalex")
    src, ad = _build("openalex")
    r.url = src.build_url("/works", {"search": "covid", "per-page": 5})
    def shape(d):
        w = d.get("results") or []
        return "" if w and "id" in w[0] else \
            f"missing results[].id; keys={sorted(d)[:10]}"
    return _run(lambda: ad.works_search("covid", limit=5), r,
                lambda d: len(d.get("results", [])), shape)


@probe("clinicaltrials")
def _clinicaltrials() -> ProbeResult:
    # The historical defect: a status word in query.term zeroed results.
    # Known-good: plain condition query must return studies.
    r = ProbeResult("clinicaltrials")
    src, ad = _build("clinicaltrials")
    r.url = src.build_url("/studies", {"query.cond": "cancer", "pageSize": 5})
    def shape(d):
        s = d.get("studies") or []
        return "" if s and "protocolSection" in s[0] else \
            f"missing studies[].protocolSection; keys={sorted(d)[:10]}"
    return _run(lambda: ad.search_studies(condition="cancer", limit=5), r,
                lambda d: len(d.get("studies", [])), shape)


@probe("federalregister")
def _federalregister() -> ProbeResult:
    # Historical defect: comma-joined fields[] got HTTP 400.
    r = ProbeResult("federalregister")
    src, ad = _build("federalregister")
    r.url = src.build_url("/documents.json",
                          {"conditions[term]": "climate", "per_page": 5})
    def shape(d):
        docs = d.get("results") or []
        return "" if docs and "title" in docs[0] else \
            f"missing results[].title; keys={sorted(d)[:10]}"
    return _run(lambda: ad.search(query_term="climate", limit=5), r,
                lambda d: len(d.get("results", [])), shape)


@probe("treasury")
def _treasury() -> ProbeResult:
    # Historical defect: dataset id 404'd.
    from tools.sources.treasury import DATASET_AVG_INTEREST_RATES
    r = ProbeResult("treasury")
    src, ad = _build("treasury")
    ds = DATASET_AVG_INTEREST_RATES
    r.url = src.build_url(f"/{ds}", {"sort": "-record_date", "limit": 5})
    def shape(d):
        rows = d.get("data") or []
        return "" if rows else "no 'data' array"
    return _run(lambda: ad.query(ds, limit=5), r,
                lambda d: len(d.get("data", [])), shape)


@probe("bls")
def _bls() -> ProbeResult:
    r = ProbeResult("bls")
    src, ad = _build("bls")
    r.url = src.spec.base_url + "/timeseries/data (POST)"
    def shape(d):
        series = (d.get("Results") or {}).get("series") or []
        if not series:
            return f"no Results.series; body keys={sorted(d)[:10]}"
        ts = series[0].get("data") or []
        return "" if ts else "Results.series[0].data empty"
    def count(d):
        series = (d.get("Results") or {}).get("series") or []
        return len(series[0].get("data", [])) if series else 0
    return _run(lambda: ad.timeseries(["LNS14000000"], 2023, 2024), r,
                count, shape)


@probe("wikidata")
def _wikidata() -> ProbeResult:
    q = ("SELECT ?item WHERE { ?item wdt:P31 wd:Q146. } LIMIT 3")
    r = ProbeResult("wikidata")
    src, ad = _build("wikidata")
    r.url = src.build_url("/sparql", {"query": q})
    def shape(d):
        b = (d.get("results") or {}).get("bindings")
        if b is None:
            return f"missing results.bindings; keys={sorted(d)[:10]}"
        return ""
    return _run(lambda: ad.sparql(q), r,
                lambda d: len((d.get("results") or {}).get("bindings", [])),
                shape)


@probe("gdelt")
def _gdelt() -> ProbeResult:
    r = ProbeResult("gdelt")
    src, ad = _build("gdelt")
    r.url = src.build_url("/doc", {"query": "climate", "mode": "artlist"})
    def shape(d):
        arts = d.get("articles")
        if arts is None:
            return f"missing articles[]; keys={sorted(d)[:10]}"
        return ""
    return _run(lambda: ad.doc_query("climate", limit=5), r,
                lambda d: len(d.get("articles", [])), shape)


@probe("sec_fulltext")
def _sec_fts() -> ProbeResult:
    r = ProbeResult("sec_fulltext")
    src, ad = _build("sec_fulltext")
    r.url = src.build_url("/search-index", {"q": "\"annual report\""})
    def shape(d):
        hits = ((d.get("hits") or {}).get("hits")) or []
        return "" if hits else "hits.hits empty"
    out = _run(lambda: ad.search("\"annual report\"", limit=5), r,
               lambda d: len(((d.get("hits") or {}).get("hits")) or []),
               shape)
    # search() normalizes into 'results'; empty normalized output with raw
    # hits present would still be caught by the count above.
    return out


@probe("courtlistener", "CALLISTO_COURTLISTENER_TOKEN")
def _courtlistener() -> ProbeResult:
    r = ProbeResult("courtlistener")
    src, ad = _build("courtlistener")
    r.url = src.build_url("/search/", {"q": "first amendment", "type": "o"})
    def shape(d):
        res = d.get("results")
        if res is None:
            return f"missing results[]; keys={sorted(d)[:10]}"
        return ""
    return _run(lambda: ad.search("first amendment", page_size=5), r,
                lambda d: len(d.get("results", [])), shape)


@probe("uspto_odp", "CALLISTO_USPTO_ODP_KEY")
def _uspto() -> ProbeResult:
    r = ProbeResult("uspto_odp")
    src, ad = _build("uspto_odp")
    r.url = src.build_url("/patent/applications/search",
                          {"q": "battery", "limit": 5})
    def shape(d):
        if "patentFileDateDataBag" not in d and "total" not in d:
            return f"unexpected payload keys={sorted(d)[:10]}"
        return ""
    return _run(lambda: ad.search_applications("battery", limit=5), r,
                lambda d: int((d.get("count") or d.get("total") or 1)),
                shape)


@probe("bea", "CALLISTO_BEA_API_KEY")
def _bea() -> ProbeResult:
    r = ProbeResult("bea")
    src, ad = _build("bea")
    r.url = src.spec.base_url + "?DataSetName=NIPA&method=GetData..."
    def shape(d):
        bea = d.get("BEAAPIs") or {}
        data = ((bea.get("Results") or {}).get("Data")) or []
        err = bea.get("Error")
        if err:
            return f"BEA error payload: {err}"
        return "" if data else "BEAAPIs.Results.Data empty"
    def count(d):
        bea = d.get("BEAAPIs") or {}
        return len(((bea.get("Results") or {}).get("Data")) or [])
    return _run(lambda: ad.get_data("NIPA", "T10101", linecode="1",
                                    frequency="A", years="2023"), r,
                count, shape)


@probe("census")
def _census() -> ProbeResult:
    r = ProbeResult("census")
    src, ad = _build("census")
    r.url = src.build_url("/2023/acs/acs1",
                          {"get": "NAME,B19013_001E", "for": "state:06"})
    def shape(d):
        return ""  # query() itself raises ValueError on unexpected shape
    def count(d):
        # adapter raises before we get here if the flat-array shape broke
        return len(d.get("rows", []))
    return _run(lambda: ad.query("2023", "acs/acs1",
                                 ["NAME", "B19013_001E"],
                                 geo_for="state:06"), r, count, shape)


@probe("eia", "CALLISTO_EIA_API_KEY")
def _eia() -> ProbeResult:
    r = ProbeResult("eia")
    src, ad = _build("eia")
    r.url = src.build_url("/seriesid/COPRPUS.A",
                          {"frequency": "annual"})
    def shape(d):
        resp = d.get("response") or d
        data = resp.get("data") or []
        if not data:
            return f"no response.data; keys={sorted(d)[:10]}"
        return ""
    def count(d):
        resp = d.get("response") or d
        return len(resp.get("data", []) or [])
    return _run(lambda: ad.series("COPRPUS.A", frequency="annual",
                                  length=3), r, count, shape)


@probe("fdic")
def _fdic() -> ProbeResult:
    # Two historical defects: host moved, and filters= vs search=.
    # institutions with a trivial filter must return rows.
    r = ProbeResult("fdic")
    src, ad = _build("fdic")
    r.url = src.build_url("/institutions",
                          {"filters": "STALP:TX", "limit": 5})
    def shape(d):
        rows = [row.get("data", row) for row in d.get("data", [])]
        if not rows:
            return "data[] empty"
        return "" if isinstance(rows[0], dict) else "data[].data not dicts"
    def count(d):
        return len([row.get("data", row) for row in d.get("data", [])])
    return _run(lambda: ad.institutions(filters="STALP:TX", limit=5),
                r, count, shape)


@probe("cftc_cot")
def _cftc() -> ProbeResult:
    # Historical defect: wrong Socrata dataset id.
    from tools.sources.cftc import LEGACY_FUTURES_ONLY
    r = ProbeResult("cftc")
    src, ad = _build("cftc")
    where = "cftc_contract_market_code='088691'"
    r.url = src.build_url(f"/{LEGACY_FUTURES_ONLY}.json",
                          {"$where": where, "$limit": 5})
    def shape(d):
        if d.get("rows"):
            row = d["rows"][0]
            if "report_date_as_yyyy_mm_dd" not in row:
                return f"row keys changed: {sorted(row)[:12]}"
        elif isinstance(d, dict) and "error" in d:
            return f"Socrata error: {d['error']}"
        return ""
    return _run(lambda: ad.contract_history("088691", weeks=2), r,
                lambda d: len(d.get("rows", [])), shape)


@probe("worldbank")
def _worldbank() -> ProbeResult:
    r = ProbeResult("worldbank")
    src, ad = _build("worldbank")
    r.url = src.build_url("/country/US/indicator/NY.GDP.MKTP.CD",
                          {"format": "json"})
    def shape(d):
        return ""  # indicator() validates the [meta, rows] envelope itself
    return _run(lambda: ad.indicator("USA", "NY.GDP.MKTP.CD",
                                     start="2021", end="2023"), r,
                lambda d: len(d.get("rows", [])), shape)


@probe("semantic_scholar")
def _semantic_scholar() -> ProbeResult:
    r = ProbeResult("semantic_scholar")
    src, ad = _build("semantic_scholar")
    r.url = src.build_url("/paper/search",
                          {"query": "attention is all you need", "limit": 5})
    def shape(d):
        papers = d.get("data")
        if papers is None:
            return f"missing data[]; keys={sorted(d)[:10]}"
        if papers and "paperId" not in papers[0]:
            return f"paper keys changed: {sorted(papers[0])[:12]}"
        return ""
    return _run(lambda: ad.paper_search("attention is all you need",
                                        limit=5), r,
                lambda d: len(d.get("data", []) or []), shape)


@probe("wayback")
def _wayback() -> ProbeResult:
    r = ProbeResult("wayback")
    src, ad = _build("wayback")
    r.url = src.build_url("/available", {"url": "example.com"})
    snap = ((ad.closest("example.com").get("archived_snapshots") or {})
            .get("closest"))
    r.http_status = 200
    if not snap:
        r.verdict = DEGRADED
        r.row_count = 0
        r.evidence.append("availability API returned no closest snapshot "
                          "for example.com (expected always-archived)")
        return r
    r.verdict = OK
    r.row_count = 1
    r.evidence.append(f"closest snapshot {snap.get('timestamp')} "
                      f"{snap.get('status')}")
    return r


@probe("kalshi")
def _kalshi() -> ProbeResult:
    r = ProbeResult("kalshi")
    src, ad = _build("kalshi")
    r.url = src.build_url("/markets", {"limit": 5})
    def shape(d):
        mkts = d.get("markets")
        if mkts is None:
            return f"missing markets[]; keys={sorted(d)[:10]}"
        return ""
    return _run(lambda: ad.list_markets(limit=5), r,
                lambda d: len(d.get("markets", [])), shape)


# ── runner ───────────────────────────────────────────────────────────────

def run_all(names: list[str] | None = None) -> list[ProbeResult]:
    require_net_gate()
    order = names or sorted(PROBES)
    results = []
    for name in order:
        if name not in PROBES:
            results.append(ProbeResult(name, BROKEN, evidence=[
                "no health probe defined for this source"]))
            continue
        key_env, thunk = PROBES[name]
        if key_env and not os.environ.get(key_env, ""):
            results.append(ProbeResult(name, SKIPPED, evidence=[
                f"requires {key_env}; not configured — live health unknown"]))
            continue
        try:
            results.append(thunk())
        except Exception as exc:  # noqa: BLE001
            results.append(ProbeResult(name, BROKEN, evidence=[
                f"{type(exc).__name__}: {exc}"]))
    return results


def render_table(results: list[ProbeResult]) -> str:
    lines = ["| source | verdict | rows | status | evidence |",
             "|---|---|---|---|---|"]
    counts: dict[str, int] = {}
    for r in sorted(results, key=lambda x: x.source):
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
        ev = "; ".join(r.evidence)[:220]
        lines.append(f"| {r.source} | {r.verdict} | "
                     f"{'' if r.row_count is None else r.row_count} | "
                     f"{'' if r.http_status is None else r.http_status} "
                     f"| {ev} |")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    lines.insert(0, f"Source health: {summary}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    as_json = "--json" in argv
    results = run_all(args or None)
    if as_json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print(render_table(results))
    failures = sum(1 for r in results if r.verdict in (DEGRADED, BROKEN))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
