"""The source-adapter pattern: one generic client, declarative specs.

A source is a SourceSpec plus query functions. The spec declares what the
registry (and the decomposer that selects sources) needs to know:

  name            unique id
  base_url        API root
  description     one line: what this source is
  answers         question types it can answer (e.g. "macro time series")
  cannot_answer   HONEST coverage limits — overstating coverage makes the
                  model stop looking when it should keep going
  tier            provenance tier 1-5 (NEXT.md §4):
                    1 primary structured, 2 primary documents,
                    3 market prices, 4 secondary analysis, 5 model priors
  min_interval_s  self-imposed minimum seconds between requests (must sit
                  comfortably under the source's stated rate limit)
  headers         required headers (User-Agent is always present; sources
                  like SEC require declared contact info)
  terms_url       terms of use / API terms link
  key_env_var     optional env var holding an API key (FRED)

RestSource.get_json(url) is the ONLY network path. It rate-limits, retries,
records every successful body into the ProvenanceLedger (primary=True,
exact URL), and returns (data, FetchRecord). Adapters stay thin; the
third adapter takes an hour, not a day, because everything cross-source
lives here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("callisto.sources")

PROVENANCE_TIERS = {
    1: "primary structured data",
    2: "primary documents",
    3: "market prices",
    4: "secondary analysis",
    5: "model priors",
}

MAX_RETRIES = 3
# A REMOTE SERVER MUST NEVER DECIDE HOW LONG WE FREEZE. OpenAlex answered 429
# with a large Retry-After; this file honoured it verbatim via a BLOCKING
# time.sleep() inside the asyncio event loop, and the entire pipeline sat for
# 6h49m at ~0% CPU — every parallel leaf frozen with it, not just this fetch.
# Retry-After is a hint from an untrusted party. Honour it, bounded.
MAX_RETRY_AFTER_S = 30


class SourceError(RuntimeError):
    """Fetch or parse failure after retries; message names the URL."""


def _default_agent() -> str:
    return os.environ.get(
        "CALLISTO_SOURCE_USER_AGENT",
        "Callisto Research Agent research@callisto.local",
    )


@dataclass(frozen=True)
class SourceSpec:
    name: str
    base_url: str
    description: str
    answers: tuple[str, ...] = ()
    cannot_answer: tuple[str, ...] = ()
    tier: int = 1
    min_interval_s: float = 0.25
    headers: tuple[tuple[str, str], ...] = ()
    terms_url: str = ""
    key_env_var: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "description": self.description,
            "answers": list(self.answers),
            "cannot_answer": list(self.cannot_answer),
            "tier": self.tier,
            "tier_meaning": PROVENANCE_TIERS.get(self.tier, "unknown"),
            "min_interval_s": self.min_interval_s,
            "terms_url": self.terms_url,
            "api_key_required": bool(self.key_env_var),
        }


class _RateLimiter:
    """Thread-safe minimum-interval limiter shared per source per process."""

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


_shared_limiter_lock = threading.Lock()
_shared_limiters: dict[tuple[str, str, float], "_RateLimiter"] = {}


def _identity_key(spec: "SourceSpec") -> tuple[str, str, float]:
    """Stable identity for shared-limiter caching.

    Keyed by (name, base_url, min_interval_s): two specs that differ in any
    of these could legitimately need different pacing, so they must not
    share. Deliberately excludes ephemeral instance identity (id(), object
    address) — those would defeat sharing entirely.
    """
    return (spec.name, spec.base_url, float(spec.min_interval_s))


def _shared_rate_limiter(spec: "SourceSpec") -> "_RateLimiter":
    key = _identity_key(spec)
    with _shared_limiter_lock:
        lim = _shared_limiters.get(key)
        if lim is None:
            lim = _RateLimiter(spec.min_interval_s)
            _shared_limiters[key] = lim
        return lim


def reset_shared_rate_limiters() -> None:
    """Test isolation hook: drop all cached shared limiters."""
    with _shared_limiter_lock:
        _shared_limiters.clear()




@dataclass
class FetchRecord:
    url: str
    status: int
    content_sha256: str
    size_bytes: int
    fetched_at: str
    user_agent: str
    duration_s: float


# transport(url, headers) -> (status, body_text). Injectable for tests.
Transport = Callable[[str, dict], "tuple[int, str]"]


class RestSource:
    """Rate-limited, provenance-recording REST client for one SourceSpec.

    `ledger` is any object exposing record_tool_result(tool_name, content,
    primary=True, urls=[url]) — agp.provenance.ProvenanceLedger satisfies
    it. Pass ledger=None only in offline tests.

    `transport` replaces the HTTP layer entirely (tests pass fixtures and
    count calls to prove no socket was opened).
    """

    def __init__(
        self,
        spec: SourceSpec,
        ledger: Any = None,
        user_agent: str = "",
        timeout_s: int = 30,
        transport: Optional[Transport] = None,
        _limiter: Optional[_RateLimiter] = None,
    ):
        self.spec = spec
        self.ledger = ledger
        self.user_agent = user_agent or _default_agent()
        self.timeout_s = timeout_s
        self._transport = transport
        # Default limiters are shared process-wide per stable source identity
        # (name + base_url + min_interval_s), so parallel leaves and
        # independently constructed clients cannot bypass each other's
        # declared min_interval_s. An explicit _limiter is an intentional
        # per-instance test/override and is NOT cached.
        self._limiter = _limiter or _shared_rate_limiter(spec)
        self.last_record: Optional[FetchRecord] = None
        self._ssl_ctx: Any = None
        if transport is None:
            try:
                import certifi  # type: ignore
                self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                self._ssl_ctx = ssl.create_default_context()
            # ClinicalTrials.gov's edge (istio-envoy + WAF) 403s the default
            # OpenSSL ClientHello: with X25519 first in the groups list every
            # request gets a bare "403 Forbidden" HTML page while the same
            # URL+headers via curl (P-256 only) returns 200. Pinning one
            # NIST curve matches the curl fingerprint and clears it
            # (verified live 2026-08-23). Servers we hit all support P-256.
            try:
                self._ssl_ctx.set_ecdh_curve("prime256v1")
            except (ValueError, AttributeError):  # pragma: no cover
                pass

    # ── headers ──────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        h = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "identity",  # hash must match body we see
        }
        for k, v in self.spec.headers:
            if k.lower() == "user-agent" and v == "{api_key}":
                continue
            h[k] = v
        key = self.api_key()
        if key:
            for k, v in self.spec.headers:
                if "{api_key}" in v:
                    h[k] = v.replace("{api_key}", key)
        return h

    def api_key(self) -> str:
        return os.environ.get(self.spec.key_env_var, "") if self.spec.key_env_var else ""

    # ── low-level fetch ──────────────────────────────────────────────────

    def _http_transport(self, url: str, headers: dict) -> tuple[int, str]:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(
            req, timeout=self.timeout_s, context=self._ssl_ctx
        ) as resp:
            return getattr(resp, "status", 200), resp.read().decode(
                "utf-8", errors="replace"
            )

    def get(self, url: str) -> tuple[int, str]:
        """Fetch url (rate-limited, retried). Returns (status, body)."""
        headers = self._headers()
        last_err: Optional[str] = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._limiter.wait()
            started = time.monotonic()
            try:
                if self._transport is not None:
                    status, body = self._transport(url, headers)
                else:
                    status, body = self._http_transport(url, headers)
                self._record(url, status, body, time.monotonic() - started)
                if status != 200:
                    # Same status semantics as the native HTTPError path
                    # below: the injected transport seam (documented for
                    # tests/offline use) must preserve GET's retry contract.
                    # 403, 429 and 5xx are transient — back off and retry;
                    # anything else is terminal. This seam has no headers,
                    # so a 403 uses only the bounded exponential fallback
                    # (the native path's max(Retry-After, 2**attempt) with
                    # no Retry-After available). The non-200 body still never
                    # reaches the ledger (_record skips it above).
                    if status == 403 or status == 429 or \
                            500 <= status < 600:
                        last_err = f"HTTP {status} for {url}"
                        time.sleep(min(2 ** attempt, MAX_RETRY_AFTER_S))
                        continue
                    raise SourceError(f"HTTP {status} for {url}") from None
                return status, body
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code} for {url}"
                if exc.code in (403, 429):
                    try:
                        retry_after = float(exc.headers.get("Retry-After", 0) or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    time.sleep(min(max(retry_after, 2 ** attempt),
                                   MAX_RETRY_AFTER_S))
                    continue
                if 500 <= exc.code < 600:
                    time.sleep(min(2 ** attempt, MAX_RETRY_AFTER_S))
                    continue
                raise SourceError(last_err) from exc
            except SourceError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = f"network error fetching {url}: {exc}"
                time.sleep(min(2 ** attempt, MAX_RETRY_AFTER_S))
        raise SourceError(f"exhausted retries; last error: {last_err}")

    def post(self, url: str, payload: dict) -> tuple[int, str]:
        """POST JSON (rate-limited, retried, recorded like GET)."""
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")

        def _do() -> tuple[int, str]:
            req = urllib.request.Request(url, data=body, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(
                req, timeout=self.timeout_s, context=self._ssl_ctx
            ) as resp:
                return getattr(resp, "status", 200), resp.read().decode(
                    "utf-8", errors="replace")

        last_err: Optional[str] = None
        for attempt in range(1, MAX_RETRIES + 1):
            self._limiter.wait()
            started = time.monotonic()
            try:
                if self._transport is not None:
                    status, text = self._transport(url, headers)
                else:
                    status, text = _do()
                self._record(url, status, text, time.monotonic() - started)
                # Non-200 wire bodies are fetch failures, never successful
                # data — same contract as get(): retry transient statuses,
                # otherwise surface a SourceError to JSON-helper callers.
                if status != 200:
                    err = f"HTTP {status} for {url}"
                    if status == 429 or 500 <= status < 600:
                        time.sleep(min(2 ** attempt, MAX_RETRY_AFTER_S))
                        last_err = err
                        continue
                    raise SourceError(err) from None
                return status, text
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code} for {url}"
                if exc.code == 429 or 500 <= exc.code < 600:
                    time.sleep(2 ** attempt)
                    continue
                raise SourceError(last_err) from exc
            except SourceError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = f"network error posting {url}: {exc}"
                time.sleep(2 ** attempt)
        raise SourceError(f"exhausted retries; last error: {last_err}")

    def post_json(self, url: str, payload: dict) -> tuple[Any, FetchRecord]:
        status, body = self.post(url, payload)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError(f"non-JSON response from {url}") from exc
        rec = self.last_record
        if rec is None or rec.url != url:
            raise SourceError(f"no fetch record for {url}")
        return data, rec

    def _record(self, url: str, status: int, body: str, dur: float) -> FetchRecord:
        rec = FetchRecord(
            url=url,
            status=status,
            content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            size_bytes=len(body.encode("utf-8")),
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            user_agent=self.user_agent,
            duration_s=round(dur, 3),
        )
        self.last_record = rec
        # Source-provenance integrity: error bytes never enter the ledger.
        # A non-200 response is a fetch failure, not an observation of the
        # world — recording it (even as SECONDARY) would let a 503 JSON/HTML
        # body mint provenance or verify a citation. The FetchRecord above
        # keeps the honest diagnostic (status, sha, url); the ledger simply
        # never hears about failed fetches.
        if status != 200:
            return rec
        if self.ledger is not None:
            # Fail closed: a successful (HTTP 200) fetch that cannot be
            # recorded is unverified evidence. Returning its body would let a
            # caller consume data with no provenance behind it, so surface a
            # SourceError identifying the source and URL instead of logging
            # and swallowing the ledger failure.
            try:
                self.ledger.record_tool_result(
                    f"{self.spec.name}_fetch", body, primary=True, urls=[url]
                )
            except Exception as exc:
                raise SourceError(
                    f"provenance ledger failed to record 200 fetch from "
                    f"{self.spec.name} ({url}): {exc}"
                ) from exc
        return rec

    def get_json(self, url: str) -> tuple[Any, FetchRecord]:
        """Fetch and parse JSON. Returns (data, FetchRecord)."""
        status, body = self.get(url)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError(f"non-JSON response from {url}") from exc
        rec = self.last_record
        if rec is None or rec.url != url:
            raise SourceError(f"no fetch record for {url}")
        return data, rec

    def build_url(self, path: str = "", params: Optional[dict] = None) -> str:
        url = self.spec.base_url.rstrip("/") + "/" + path.lstrip("/")
        if params:
            # doseq: repeated keys for list values (e.g. fields[]=a,
            # fields[]=b) — the Federal Register API 400s on comma-joined
            # field lists.
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        return url


# ── Independence families (I2) ────────────────────────────────────────────
#
# Two adapters that index or resell the same underlying corpus are ONE
# independent source no matter how different their APIs look. Declaring
# that here — next to the specs that know what each source actually is —
# keeps the honest answer with the adapter layer; consumers collapse on
# it rather than re-deriving (or inflating) independence per pipeline.
#
# Membership is deliberately small: every entry claims "these do not
# corroborate each other", which LOWERS confidence ceilings when two
# family members both hit. That is the safe direction; omitting a real
# overlap is the defect (it inflates confidence).
INDEPENDENCE_FAMILIES: dict[str, frozenset[str]] = {
    # OpenAlex and Semantic Scholar both index the scholarly literature
    # with heavily overlapping crawl bases; a paper findable in one is
    # nearly always findable in the other.
    "scholarly-aggregator": frozenset({"openalex", "semanticscholar"}),
}


def independence_family(spec_name: str) -> str:
    """The family key a source counts as toward min_independent_sources:
    its declared family name, or its own name when it stands alone."""
    for family, members in INDEPENDENCE_FAMILIES.items():
        if spec_name in members:
            return family
    return spec_name
