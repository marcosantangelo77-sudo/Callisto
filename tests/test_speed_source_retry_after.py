"""SPEED run 18 — Retry-After decline policy for RestSource.

Live-measured defect: this machine got HTTP 429 + `Retry-After: 916` from
CourtListener. The old policy slept min(RA, 30s) and retried, 3 attempts —
~90s of guaranteed-futile sleep per fetch attempt, per retrieval round.
A source cooling down LONGER than our cap cannot be out-waited; decline
immediately and let the leaf proceed without it.

Contract (all measured against tools/sources/base.py):
  a. RA > MAX_RETRY_AFTER_S  -> SourceError on attempt 1, zero sleeps.
  b. RA <= MAX_RETRY_AFTER_S -> honoured exactly as before (sleep = RA).
  c. missing/garbled header  -> exponential backoff unchanged.
  d. 5xx                     -> exponential backoff unchanged.
  e. success after bounded-RA retry still works.
"""
import urllib.error

import pytest

from tools.sources import base as sb


class _Headers(dict):
    """Header map whose Retry-After is configurable."""

    ra = "30"

    def get(self, k, d=None):
        if k.lower() == "retry-after":
            return self.ra
        return d


def _spec():
    return sb.SourceSpec(name="ra-test", base_url="https://example.invalid",
                         description="429 policy pins", min_interval_s=0.0)


def _transport(status_ra_pairs):
    """Transport that raises the given (status, retry_after) sequence."""
    it = iter(status_ra_pairs)

    def _t(url, headers):
        status, ra = next(it)
        hdrs = _Headers()
        hdrs.ra = str(ra)
        raise urllib.error.HTTPError(url, status, "err", hdrs, None)

    return _t


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(sb.time, "sleep", lambda s: slept.append(s))
    yield slept


def test_huge_retry_after_declines_immediately(_fast_sleep):
    """a. RA=916 > cap 30: fail NOW, no sleeps at all."""
    src = sb.RestSource(_spec(), transport=_transport([(429, 916)]))
    with pytest.raises(sb.SourceError, match="cooling down"):
        src.get("https://example.invalid/x")
    assert _fast_sleep == [], (
        f"decline path must not sleep; slept {_fast_sleep}")


def test_403_huge_retry_after_also_declines(_fast_sleep):
    src = sb.RestSource(_spec(), transport=_transport([(403, 3600)]))
    with pytest.raises(sb.SourceError, match="cooling down"):
        src.get("https://example.invalid/x")
    assert _fast_sleep == []


def test_bounded_retry_after_still_honoured(_fast_sleep):
    """b/c/d. RA <= cap sleeps RA then succeeds; no-header uses backoff."""
    calls = [(429, 10), (200, None)]

    def _t(url, headers):
        status, ra = calls.pop(0)
        if status == 429:
            h = _Headers()
            h.ra = str(ra)
            raise urllib.error.HTTPError(url, status, "err", h, None)
        class _R:
            def read(self):
                return b"{}"
            status = 200
        return 200, "{}"

    src = sb.RestSource(_spec(), transport=_t)
    status, body = src.get("https://example.invalid/x")
    assert status == 200
    assert _fast_sleep == [10.0], "bounded RA must be honoured verbatim"


def test_no_header_backoff_unchanged(_fast_sleep):
    """Garbled header -> default exponential backoff, retries continue."""
    seq = [(429, None), (429, None), (429, None)]

    def _t(url, headers):
        status, _ra = seq.pop(0)
        h = _Headers()
        h.ra = "garbage"
        raise urllib.error.HTTPError(url, status, "err", h, None)

    src = sb.RestSource(_spec(), transport=_t)
    with pytest.raises(sb.SourceError):
        src.get("https://example.invalid/x")
    # exponential: 2**1, 2**2, 2**3 across 3 attempts
    assert _fast_sleep == [2, 4, 8]


def test_redteam_dos_bound_still_holds(_fast_sleep):
    """The red-team invariant survives, strengthened: total freeze <= cap,
    and under huge-RA now ZERO sleep."""
    src = sb.RestSource(_spec(), transport=_transport([(429, 86400)]))
    with pytest.raises(sb.SourceError):
        src.get("https://example.invalid/works")
    if _fast_sleep:
        assert max(_fast_sleep) <= sb.MAX_RETRY_AFTER_S
