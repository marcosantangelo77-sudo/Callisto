"""SEC EDGAR structured-data fetcher — tier-1 primary source (NEXT.md §4).

Everything here reads the SEC's machine-readable endpoints, never prose:

    https://www.sec.gov/files/company_tickers.json   ticker → CIK map
    https://data.sec.gov/submissions/CIK##########.json  filing index
    https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json  all tagged facts
    https://data.sec.gov/api/xbrl/companyconcept/CIK#/us-gaap/Tag.json one concept

Facts arrive as XBRL-tagged values with units, periods (start/end or
instant), accession number, form, fiscal year/period, and frame attached —
a number in a model traces to the filing fact it came from.

Two SEC rules are explicit and both are enforced here:
  - User-Agent: every request carries a declared UA with contact info
    (override with CALLISTO_SEC_USER_AGENT; the default is a placeholder
    that the deployer SHOULD replace).
  - Rate limit: max 10 req/s declared; we self-limit to ~4 req/s with a
    minimum inter-request gap plus Retry-After handling on 403/429.

Provenance: every successful fetch is recorded in agp.provenance's
ProvenanceLedger via record_tool_result(primary=True) with the exact URL,
so a fact's bytes-hash is verifiable against what the wire actually
returned. A model built from these facts inherits PRIMARY provenance by
construction — not by label.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("callisto.edgar")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
COMPANYCONCEPT_URL = (
    "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{tag}.json"
)

DEFAULT_MIN_INTERVAL_S = 0.25  # ≈4 req/s, comfortably under SEC's 10/s ceiling
MAX_RETRIES = 3


class EdgarError(RuntimeError):
    """Raised for fetch failures after retries; message says which URL failed."""


def _default_agent() -> str:
    return os.environ.get(
        "CALLISTO_SEC_USER_AGENT",
        "Callisto Research Agent research@callisto.local",
    )


class _RateLimiter:
    """Thread-safe minimum-interval limiter shared per process."""

    def __init__(self, min_interval_s: float):
        self.min_interval_s = min_interval_s
        self._next_ok = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next_ok:
                    self._next_ok = now + self.min_interval_s
                    return
                delay = self._next_ok - now
            time.sleep(min(delay, 1.0))


@dataclass
class FetchRecord:
    """What came back, hashed, so a downstream model can cite exact bytes."""

    url: str
    status: int
    content_sha256: str
    size_bytes: int
    fetched_at: str
    user_agent: str
    duration_s: float


@dataclass
class EdgarClient:
    """Rate-limited SEC fetcher with provenance recording.

    `ledger` is any object exposing ``record_tool_result(tool_name, content,
    primary=True, urls=[url])`` — agp.provenance.ProvenanceLedger satisfies it.
    Pass ledger=None only in offline tests.
    """

    ledger: Any = None
    user_agent: str = field(default_factory=_default_agent)
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    timeout_s: int = 30
    _limiter: _RateLimiter = field(init=False, repr=False)
    _ticker_map: Optional[dict[str, dict]] = field(
        default=None, init=False, repr=False
    )
    _ssl_ctx: Any = field(default=None, init=False, repr=False)
    _last_record: Optional[FetchRecord] = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._limiter = _RateLimiter(self.min_interval_s)
        try:
            import certifi  # type: ignore

            self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            self._ssl_ctx = ssl.create_default_context()

    # ── low-level fetch ──────────────────────────────────────────────────

    def _get(self, url: str) -> tuple[int, str]:
        last_err: Optional[str] = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._limiter.wait()
            started = time.monotonic()
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",  # hash must match body we see
                },
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=self.timeout_s, context=self._ssl_ctx
                ) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    status = getattr(resp, "status", 200)
                self._record(url, status, body, time.monotonic() - started)
                return status, body
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code} for {url}"
                if exc.code in (403, 429):
                    retry_after = float(exc.headers.get("Retry-After", 0) or 0)
                    time.sleep(max(retry_after, 2**attempt))
                    continue
                if 500 <= exc.code < 600:
                    time.sleep(2**attempt)
                    continue
                raise EdgarError(last_err) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = f"network error fetching {url}: {exc}"
                time.sleep(2**attempt)
        raise EdgarError(f"exhausted retries; last error: {last_err}")

    def _record(self, url: str, status: int, body: str, dur: float) -> FetchRecord:
        import hashlib

        rec = FetchRecord(
            url=url,
            status=status,
            content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            size_bytes=len(body.encode("utf-8")),
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            user_agent=self.user_agent,
            duration_s=round(dur, 3),
        )
        self._last_record = rec
        if self.ledger is not None:
            try:
                self.ledger.record_tool_result(
                    "edgar_fetch", body, primary=True, urls=[url]
                )
            except Exception:  # pragma: no cover - ledger must not break fetches
                logger.exception("provenance ledger rejected edgar observation")
        return rec

    def _get_json(self, url: str) -> tuple[Any, FetchRecord]:
        status, body = self._get(url)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise EdgarError(f"non-JSON response from {url}") from exc
        rec = self._last_record
        if rec is None or rec.url != url:
            raise EdgarError(f"no fetch record for {url}")
        return data, rec

    # ── public endpoints ─────────────────────────────────────────────────

    def ticker_to_cik(self, ticker: str) -> int:
        """Resolve a ticker to its SEC CIK via the official mapping file."""
        t = ticker.strip().upper()
        if self._ticker_map is None:
            data, _rec = self._get_json(TICKERS_URL)
            self._ticker_map = {
                item["ticker"].upper(): item for item in data.values()
            }
        entry = self._ticker_map.get(t)
        if not entry:
            raise EdgarError(f"unknown ticker {t!r} (not in SEC company_tickers)")
        return int(entry["cik_str"])

    def companyfacts(self, cik: int) -> dict:
        url = COMPANYFACTS_URL.format(cik=cik)
        data, rec = self._get_json(url)
        data["_fetch"] = {
            "url": rec.url,
            "sha256": rec.content_sha256,
            "fetched_at": rec.fetched_at,
        }
        return data

    def companyconcept(self, cik: int, taxonomy: str, tag: str) -> dict:
        url = COMPANYCONCEPT_URL.format(cik=cik, taxonomy=taxonomy, tag=tag)
        data, rec = self._get_json(url)
        data["_fetch"] = {
            "url": rec.url,
            "sha256": rec.content_sha256,
            "fetched_at": rec.fetched_at,
        }
        return data

    def submissions(self, cik: int) -> dict:
        url = SUBMISSIONS_URL.format(cik=cik)
        data, rec = self._get_json(url)
        return data

    def facts_for_ticker(self, ticker: str) -> tuple[int, dict]:
        """Convenience: ticker → (cik, companyfacts). Two rate-limited calls."""
        cik = self.ticker_to_cik(ticker)
        return cik, self.companyfacts(cik)


# ── fact extraction helpers (pure, unit-tested offline) ───────────────────


def concept_units(facts: dict, tag: str, taxonomy: str = "us-gaap") -> dict:
    """units dict {unit_name: [fact,...]} for a tag across taxonomies, or {}."""
    taxonomies = facts.get("facts", {})
    if taxonomy in taxonomies and tag in taxonomies[taxonomy]:
        return taxonomies[taxonomy][tag].get("units", {})
    # some filers put revenue under ifrs-full or dei; caller may retry there
    return {}


def annual_facts(
    facts: dict,
    tag: str,
    *,
    taxonomy: str = "us-gaap",
    form_filter: Optional[set[str]] = None,
) -> list[dict]:
    """Annual-duration facts for a tag, deduplicated to one value per period.

    Selection rules that handle real-filer mess:
      - keep only facts with BOTH start and end (duration facts), spanning
        roughly a year (330–400 days) → annual figures;
      - dedupe by (start, end): prefer the most recent filing (highest
        accn/filed) — this picks up RESTATED values over originals, which is
        the correct default for building current-period statements;
      - optionally restrict to forms (e.g. {"10-K"}).

    Each returned fact keeps: start, end, val, accn, fy, fp, form, filed,
    frame (when present). The caller can therefore always say WHICH filing a
    number came from.
    """
    out_by_period: dict[tuple[str, str], dict] = {}
    for unit_name, flist in concept_units(facts, tag, taxonomy).items():
        for f in flist:
            start, end = f.get("start"), f.get("end")
            if not start or not end:
                continue  # instant fact, not a duration figure
            try:
                span = (
                    time.mktime(time.strptime(end, "%Y-%m-%d"))
                    - time.mktime(time.strptime(start, "%Y-%m-%d"))
                ) / 86400.0
            except (ValueError, TypeError, OverflowError):
                continue
            if not (330 <= span <= 400):
                continue
            if form_filter and f.get("form") not in form_filter:
                continue
            key = (start, end)
            cand = dict(f)
            cand["unit"] = unit_name
            prior = out_by_period.get(key)
            if prior is None or str(cand.get("filed", "")) > str(prior.get("filed", "")):
                out_by_period[key] = cand
    return sorted(out_by_period.values(), key=lambda f: f["end"])


def instant_facts(
    facts: dict,
    tag: str,
    *,
    taxonomy: str = "us-gaap",
    form_filter: Optional[set[str]] = None,
) -> list[dict]:
    """Instant (point-in-time) facts for a tag, latest filing per date.

    Balance-sheet items (Assets, Liabilities, StockholdersEquity, ...) are
    instants. Dedupe by end date preferring the most recent filing, so a
    restated balance sheet wins over the original.
    """
    out_by_date: dict[str, dict] = {}
    for unit_name, flist in concept_units(facts, tag, taxonomy).items():
        for f in flist:
            end = f.get("end")
            if not end or f.get("start"):
                continue
            if form_filter and f.get("form") not in form_filter:
                continue
            cand = dict(f)
            cand["unit"] = unit_name
            prior = out_by_date.get(end)
            if prior is None or str(cand.get("filed", "")) > str(prior.get("filed", "")):
                out_by_date[end] = cand
    return sorted(out_by_date.values(), key=lambda f: f["end"])
