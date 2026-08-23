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
    slept = []
    monkeypatch.setattr(sb.time, "sleep", lambda s: slept.append(s))
    src = sb.RestSource(_spec(), transport=_always_429)

    with pytest.raises(sb.SourceError):
        src.get("https://example.invalid/works")

    # The retry path MUST have been exercised, or this test proves nothing.
    assert slept, "no backoff sleep recorded — test never reached the retry path"
    assert max(slept) <= sb.MAX_RETRY_AFTER_S, (
        f"slept {max(slept)}s because a server asked for {HUGE}s")


def test_cap_constant_is_real_and_unbounded_sleep_is_gone():
    src = open(sb.__file__).read()
    assert sb.MAX_RETRY_AFTER_S <= 60
    assert "time.sleep(max(retry_after, 2 ** attempt))" not in src, \
        "unbounded Retry-After sleep is back"
