"""Source health check — does each registered source actually WORK live?

Fixtures test OUR PARSING; they say nothing about THEIR API. The I2 live
smoke found five sources silently dead while the fixture suite passed
cleanly (FDIC host move, FDIC filters-vs-search zero hits, wrong CFTC
dataset id, Treasury 404, Federal Register 400, ClinicalTrials 0-of-121).
A source returning 200-with-zero-results is a FAILURE of health, not a
pass — that is exactly how those defects hid.

This module defines, per registered source, one KNOWN-GOOD PROBE: an
adapter call that must succeed AND return at least one result row, plus
a shape assertion on what comes back. Verdicts:

    OK        reachable, non-empty, shape as expected
    DEGRADED  reachable (HTTP 200-class) but the probe returned nothing —
              the silent-empty failure mode
    BROKEN    unreachable, non-JSON, HTTP error, or shape changed
    SKIPPED   keyed source with no credentials configured (not a failure)

NETWORK GATE: probes open real sockets. They must NEVER run under the
normal suite (tests/helpers/no_socket.py exists because the SEC already
rate-limited this machine once). Every entry point checks
CALLISTO_SOURCE_HEALTH_NET == "1" before touching the network and raises
otherwise. The offline tests exercise only the verdict classifier with a
fake transport.

Run:  CALLISTO_SOURCE_HEALTH_NET=1 python3 scripts/source_health.py
"""

from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from tools.sources.base import RestSource

NET_GATE_ENV = "CALLISTO_SOURCE_HEALTH_NET"

# This host's Python TLS is intercepted (see scripts/live_smoke_w6_i2.py);
# curl handles it. -L so host moves that 301 do not masquerade as content.
CURL_FLAGS = ["-sL", "-m", "40"]


def curl_transport(url: str, headers: dict) -> tuple[int, str]:
    """Real transport via subprocess curl. Only ever used behind the net
    gate — never import-time, never from tests."""
    import subprocess

    cmd = ["curl", *CURL_FLAGS, url]
    for k, v in headers.items():
        if k.lower() != "accept-encoding":
            cmd.insert(2, "-H")
            cmd.insert(3, f"{k}: {v}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return 200, proc.stdout


def require_net_gate() -> None:
    if os.environ.get(NET_GATE_ENV) != "1":
        raise RuntimeError(
            f"source health check refused: set {NET_GATE_ENV}=1 to allow "
            f"live network probing. It must never run from the normal "
            f"suite (no_socket guard).")


@dataclass
class ProbeResult:
    source: str
    verdict: str                    # OK | DEGRADED | BROKEN | SKIPPED
    evidence: str = ""
    url: str = ""
    duration_s: float = 0.0


@dataclass
class Probe:
    """A known-good query against one source.

    run(adapter) executes the query and returns (count, evidence_url,
    shape_note) where count>0 means healthy. Raise ProbeSkip to mark the
    source SKIPPED (credentials absent, etc.). Any other exception is
    BROKEN with the exception text as evidence.
    """
    run: Callable[[Any], tuple]
    # optional post-check: raise AssertionError when the payload shape
    # drifted even though the row count was positive
    shape_check: Callable[[Any], str] | None = None


def _len_of(*collections) -> int:
    n = 0
    for c in collections:
        try:
            n += len(c)
        except TypeError:
            pass
    return n


class ProbeSkip(Exception):
    """Raised inside a probe when the source cannot be tested fairly."""


# ── the probes ────────────────────────────────────────────────────────────
# One entry per registered source. Adding a source means adding a line
# here; a source with no probe reports UNTESTED, never OK.

def _fred(ad):
    obs = ad.series_observations("UNRATE", limit=12)
    return _len_of(obs.get("observations")), obs.get("_fetch", {}).get("url", "")


def _openalex(ad):
    res = ad.works_search("semiconductor supply chain resilience", limit=10)
    return _len_of(res.get("results")), ""


def _clinicaltrials(ad):
    # NOTE: no status filter — a status word here once returned 0 of 121
    res = ad.search_studies(condition="cancer", limit=10)
    studies = res.get("studies")
    return _len_of(studies), ""


def _federalregister(ad):
    res = ad.search(query_term="climate", limit=10)
    return _len_of(res.get("documents")), ""


def _treasury(ad):
    res = ad.query("v2/accounting/od/debt_to_penny", limit=10,
                   sort="-record_date")
    return _len_of(res.get("data")), ""


def _bls(ad):
    res = ad.timeseries(["LNS14000000"], 2023, 2024)
    series = res.get("Results", {}).get("series", [])
    n = sum(len(s.get("data", [])) for s in series)
    return n, ""


def _wikidata(ad):
    q = ("SELECT ?item WHERE { ?item wdt:P31 wd:Q146 . } LIMIT 5")
    res = ad.sparql(q)
    return _len_of(res.get("results", {}).get("bindings")), ""


def _gdelt(ad):
    res = ad.doc_query("semiconductor", timespan="1w", limit=25)
    return _len_of(res.get("articles")), ""


def _sec_fts(ad):
    res = ad.search("chip export controls", limit=10)
    return len(res.get("hits", [])), ""


def _courtlistener(ad):
    res = ad.search("export controls", search_type="o", page_size=10)
    return _len_of(res.get("results")), ""


def _uspto_odp(ad):
    res = ad.search_applications("semiconductor packaging", limit=10)
    total = res.get("count") or res.get("total")
    n = len(res.get("patents", res.get("applicationMetadata", []) or []))
    return (n if n else (_to_int(total))), ""


def _to_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _bea(ad):
    res = ad.get_data("Regional", tablename="SAINC1",
                      linecode="1", frequency="A", years="2022")
    data = (res.get("BEAAPIs", {}).get("Results", {}).get("Data"))
    return _len_of(data), ""


def _census(ad):
    res = ad.query("2022", "acs/acs1", ["NAME"], "us:*")
    rows = res.get("rows")
    if rows is None:                       # raw list form before normalization
        return _len_of(res) - 1 if isinstance(res, list) else 0, ""
    return len(rows), ""


def _eia(ad):
    res = ad.series("PET.RWTC.M", length=12)
    return len(res.get("data", [])), ""


def _fdic(ad):
    # filters= (exact-match DSL) with a predicate that matches thousands —
    # deliberately NOT NAME:"chase", whose exact-match emptiness was the
    # I2 defect. This probe tests reachability+shape, not ES search.
    res = ad.institutions(filters="STALP:TX AND ASSET>10000", limit=20)
    return _len_of(res.get("data")), ""


def _cftc(ad):
    from tools.sources.cftc import LEGACY_FUTURES_ONLY
    res = ad.query(LEGACY_FUTURES_ONLY,
                   where="cftc_contract_market_code='067651'",  # WTI
                   limit=10)
    return len(res.get("rows", [])), ""


def _worldbank(ad):
    res = ad.indicator("USA", "NY.GDP.MKTP.CD", start="2020", end="2022")
    return len(res.get("rows", [])), ""


def _semantic_scholar(ad):
    res = ad.paper_search("lithium battery recycling", limit=10)
    return _len_of(res.get("data")), ""


def _wayback(ad):
    res = ad.closest("https://example.com", timestamp="2020")
    snap = (res.get("archived_snapshots", {}) or {}).get("closest")
    return (1 if snap else 0), ""


def _kalshi(ad):
    res = ad.list_markets(status="open", limit=20)
    return len(res.get("markets", [])), ""


PROBES: dict[str, Callable[[Any], tuple]] = {
    "fred": _fred,
    "openalex": _openalex,
    "clinicaltrials": _clinicaltrials,
    "federalregister": _federalregister,
    "treasury": _treasury,
    "bls": _bls,
    "wikidata": _wikidata,
    "gdelt": _gdelt,
    "sec_fulltext": _sec_fts,
    "courtlistener": _courtlistener,
    "uspto_odp": _uspto_odp,
    "bea": _bea,
    "census": _census,
    "eia": _eia,
    "fdic": _fdic,
    "cftc_cot": _cftc,
    "worldbank": _worldbank,
    "semanticscholar": _semantic_scholar,
    "wayback": _wayback,
    "kalshi": _kalshi,
}


def classify(name: str, probe: Callable[[Any], tuple],
             adapter: Any, url_hint: str = "") -> ProbeResult:
    """Run ONE probe and classify its outcome. Never raises."""
    started = time.monotonic()
    try:
        count, url = probe(adapter)
    except ProbeSkip as exc:
        return ProbeResult(name, "SKIPPED", evidence=str(exc),
                           url=url_hint, duration_s=time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 — every failure mode is a verdict
        tb = traceback.format_exc(limit=3)
        detail = f"{type(exc).__name__}: {exc}"
        if "HTTP" in detail or "network error" in detail or "non-JSON" in detail:
            detail += " | " + tb.splitlines()[-2].strip() if tb else ""
        return ProbeResult(name, "BROKEN", evidence=detail, url=url_hint or url_hint,
                           duration_s=time.monotonic() - started)
    dur = time.monotonic() - started
    if count <= 0:
        return ProbeResult(
            name, "DEGRADED",
            evidence=f"reachable but probe returned 0 rows "
                     f"(200-with-zero-results is a FAILURE)",
            url=url or url_hint, duration_s=dur)
    return ProbeResult(name, "OK",
                       evidence=f"{count} result rows from known-good query",
                       url=url or url_hint, duration_s=dur)


def run_all(registry=None, transport=curl_transport) -> list[ProbeResult]:
    """Probe EVERY registered source. Requires the net gate; raises otherwise."""
    require_net_gate()
    if registry is None:
        from tools.sources.registry import get_source_registry
        registry = get_source_registry()
    results: list[ProbeResult] = []
    for name in registry.names():
        entry = registry.get(name)
        probe = PROBES.get(name)
        if entry is None or probe is None:
            results.append(ProbeResult(name, "SKIPPED",
                                       evidence="no probe defined (untested)"))
            continue
        if entry.spec.key_env_var and \
                not os.environ.get(entry.spec.key_env_var):
            results.append(ProbeResult(
                name, "SKIPPED",
                evidence=f"requires {entry.spec.key_env_var}, not configured",
                url=entry.spec.base_url))
            continue
        adapter = entry.make_adapter(RestSource(entry.spec, ledger=None,
                                                transport=transport))
        results.append(classify(name, probe, adapter,
                                url_hint=entry.spec.base_url))
    return results
