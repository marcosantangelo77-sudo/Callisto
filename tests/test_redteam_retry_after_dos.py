"""A remote server must not be able to freeze the pipeline.

Reproduction of a real 6h49m hang: OpenAlex answered 429 with a large
Retry-After, tools/sources/base.py honoured it verbatim through a BLOCKING
time.sleep() inside the asyncio event loop, and the whole run — every parallel
leaf, not just this fetch — sat at ~0% CPU until killed, socket in CLOSE_WAIT.
"""
import urllib.error
import pytest
from tools.sources import base as sb

HUGE = "86400"  # 24 hours, what a rate-limited host may legitimately send


class _Headers(dict):
    def get(self, k, d=None):
        return HUGE if k == "Retry-After" else d


def _spec():
    return sb.SourceSpec(name="openalex-test", base_url="https://example.invalid",
                         description="429 machine", min_interval_s=0.0)


def _always_429(url, headers):
    raise urllib.error.HTTPError(url, 429, "Too Many Requests", _Headers(), None)


def test_huge_retry_after_does_not_become_a_huge_sleep(monkeypatch):
    # SPEED run 18 (2026-08-25): the policy this test defends got STRICTER.
    # A Retry-After larger than MAX_RETRY_AFTER_S now declines immediately
    # (zero sleeps) instead of sleeping the cap and retrying — a server
    # cooling down for 916s (CourtListener, live-measured) cannot be
    # out-waited by three 30s naps. The freeze bound is preserved and
    # tightened from <=90s to ~0s; see tests/test_speed_source_retry_after.py.
    slept = []
    monkeypatch.setattr(sb.time, "sleep", lambda s: slept.append(s))
    src = sb.RestSource(_spec(), transport=_always_429)

    with pytest.raises(sb.SourceError):
        src.get("https://example.invalid/works")

    # The DoS bound holds absolutely: whatever we slept is capped...
    assert all(s <= sb.MAX_RETRY_AFTER_S for s in slept), (
        f"slept {max(slept) if slept else 0}s because a server asked for {HUGE}s")
    # ...and under huge-RA the decline path sleeps NOTHING at all.
    assert not slept, "huge Retry-After should decline immediately"


def test_cap_constant_is_real_and_unbounded_sleep_is_gone():
    src = open(sb.__file__).read()
    assert sb.MAX_RETRY_AFTER_S <= 60
    assert "time.sleep(max(retry_after, 2 ** attempt))" not in src, \
        "unbounded Retry-After sleep is back"
