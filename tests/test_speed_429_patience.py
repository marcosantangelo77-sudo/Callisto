"""SPEED run 15 — patience budget must bind BEFORE attempt count, and
rate-limit exhaustion must not poison a healthy endpoint's health state.

Two measured defects (2026-08-24 night, production shape, live pricing):

1. With attempts=2 at most ONE Retry-After window could ever be honoured:
   attempt 1 sleeps RA:30, attempt 2 sees another 429 and the budget check
   (30+30 > 35) raises. Portal under load serves CONSECUTIVE ~30s windows
   (raw-curl controls measured stacks of 60–128s), so those calls abandoned
   the warm proxy and paid sequential patience-then-fork (35s spent, then a
   48–78s fresh-fork CLI call queuing on the SAME upstream capacity).
   Fix: _429_PATIENCE_S=120 with attempts=5 — the BUDGET is the binding
   constraint, four RA:30 windows fit, five do not.

2. Budget exhaustion raised a plain HTTPStatusError, so
   ProviderRouter.complete() called record_failure() and ratcheted the
   HEALTHY proxy's exponential cooldown (2s→…→60s cap) for a condition
   shared by every tier. Sustained pressure locks later calls out of the
   fast path entirely. Fix: budget exhaustion raises _RateLimitExhausted;
   complete() applies a short FLAT cooldown (no escalation, consecutive-
   failure count untouched) and fails over this call as before.

No caching anywhere near a cutoff; the adversary keeps its own call; no gate
moved — only WHEN/WHERE the identical completion is served.

Pins:
1. two stacked Retry-After:30 windows retried in place to success.
2. budget arithmetic exact: four 30s windows fit (120s slept), fifth declines;
   raises _RateLimitExhausted; total requested sleep == budget.
3. hostile huge Retry-After declined immediately (no sleep, one attempt).
4. non-429 4xx immediate HTTPStatusError, untouched.
5. small Retry-After path unchanged from run 8/9/10.
6. through ProviderRouter.complete(): _RateLimitExhausted leaves
   consecutive_failures at 0 and applies a flat, NON-escalating cooldown,
   while failover to the next tier still happens.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference  # noqa: E402


def _endpoint() -> inference.EndpointConfig:
    return inference.EndpointConfig(
        name="ox_alpha_proxy", backend="openai_compat",
        base_url="http://127.0.0.1:1/v1", model="stealth/ox-alpha")


class _Resp:
    def __init__(self, status: int, retry_after: str = ""):
        self.status_code = status
        self.headers = {"Retry-After": retry_after} if retry_after else {}


class _Err(inference.httpx.HTTPStatusError):
    def __init__(self, status: int, retry_after: str = ""):
        self.response = _Resp(status, retry_after)
        super().__init__("x", request=None, response=self.response)  # type: ignore[arg-type]


class _FakeSleep:
    """Replaces time.sleep inside _post_with_retry: instant, but records
    every requested duration so budget behaviour is pinned without paying
    minutes of wall clock."""

    def __init__(self):
        self.requested: list[float] = []

    async def __call__(self, seconds: float):
        self.requested.append(seconds)


class _ShimAsyncio:
    """Delegates to real asyncio except sleep."""

    def __init__(self, real, fake_sleep):
        self._real = real
        self.sleep = fake_sleep

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def fast_sleep(monkeypatch):
    fake = _FakeSleep()
    monkeypatch.setattr(inference, "_asyncio", _ShimAsyncio(asyncio, fake))
    return fake


def _post_factory(failures: list[_Err], ok_result=("OK", {})):
    calls = {"n": 0}

    async def post_fn(endpoint, payload, timeout):
        n = calls["n"]
        calls["n"] += 1
        if n < len(failures):
            raise failures[n]
        return ok_result

    post_fn.calls = calls  # type: ignore[attr-defined]
    return post_fn


@pytest.mark.asyncio
async def test_two_stacked_30s_windows_retried_in_place_to_success(fast_sleep):
    """THE live pattern: Portal answers 429 RA:30 twice, then serves. Under
    run-10 code this raised after ONE window (budget 35s, attempts 2) and
    the call fell to a fresh fork. Now both windows are honoured in place."""
    post = _post_factory([_Err(429, "30"), _Err(429, "30")])
    res = await inference._post_with_retry(post, _endpoint(), {}, 300)
    assert res[0] == "OK"
    assert post.calls["n"] == 3                      # two in-place retries
    assert fast_sleep.requested == [30.0, 30.0]      # both windows honoured
    assert sum(fast_sleep.requested) <= inference._429_PATIENCE_S


@pytest.mark.asyncio
async def test_budget_binds_not_attempts_exhaustion_type_is_distinct(fast_sleep):
    """Five consecutive RA:30 windows: four honoured (30+30+30+30 = 120 =
    budget exactly), the fifth declines. Raises _RateLimitExhausted — the
    type that spares the healthy endpoint its exponential cooldown — not a
    plain HTTPStatusError."""
    post = _post_factory([_Err(429, "30")] * 5)
    t0 = time.monotonic()
    with pytest.raises(inference._RateLimitExhausted):
        await inference._post_with_retry(post, _endpoint(), {}, 300)
    elapsed = time.monotonic() - t0
    assert post.calls["n"] == 5                      # attempts exhausted...
    assert fast_sleep.requested == [30.0] * 4        # ...but budget bound first
    assert sum(fast_sleep.requested) == pytest.approx(
        inference._429_PATIENCE_S)
    assert elapsed < 5.0                             # fake sleep: no wall wait


@pytest.mark.asyncio
async def test_hostile_retry_after_above_patience_declined_immediately(fast_sleep):
    post = _post_factory([_Err(429, "100000")])
    with pytest.raises(inference._RateLimitExhausted):
        await inference._post_with_retry(post, _endpoint(), {}, 60,
                                         attempts=2)
    assert post.calls["n"] == 1                      # no in-place retry at all
    assert fast_sleep.requested == []                # never slept


@pytest.mark.asyncio
async def test_non429_4xx_fails_over_immediately(fast_sleep):
    post = _post_factory([_Err(403)])
    with pytest.raises(inference.httpx.HTTPStatusError):
        await inference._post_with_retry(post, _endpoint(), {}, 60,
                                         attempts=5)
    assert post.calls["n"] == 1
    assert fast_sleep.requested == []


@pytest.mark.asyncio
async def test_small_retry_after_path_unchanged(fast_sleep):
    post = _post_factory([_Err(429, "2")])
    res = await inference._post_with_retry(post, _endpoint(), {}, 60,
                                           attempts=2)
    assert res[0] == "OK"
    assert fast_sleep.requested == [2.0]


class TestHealthNotPoisoned:
    """Run 15 pin 6: through the router, rate-limit exhaustion must NOT
    escalate the endpoint's exponential cooldown."""

    def _router(self, monkeypatch):
        r = inference.ProviderRouter()
        # silence every tier except proxy (raised by the stub) and CLI (faked)
        for name in ("gpu1", "gpu1_fast", "frontier"):
            if name in r.states:
                for _ in range(4):
                    r.states[name].record_failure()
        return r

    def test_rate_limit_exhaustion_flat_cooldown_no_health_escalation(
            self, monkeypatch):
        r = self._router(monkeypatch)

        async def always_rate_limited(endpoint, payload, timeout, attempts=5):
            raise inference._RateLimitExhausted("budget spent")

        monkeypatch.setattr(inference, "_post_with_retry",
                            always_rate_limited)

        import tools.pipeline.hermes_cli as hc

        class FakeCli:
            async def complete(self, messages, *, role="", binary=None,
                               cwd="/tmp", timeout_s=240.0):
                return {"content": "OK", "rc": 0, "stderr": ""}

        monkeypatch.setattr(hc, "hermes_complete", FakeCli().complete)

        res = asyncio.run(r.complete(
            "research_synthesis",
            [{"role": "user", "content": "Reply OK"}]))
        assert res["tier"] == "ox_alpha"             # failed over this call
        st = r.states["ox_alpha_proxy"]
        assert st.consecutive_failures == 0          # health NOT poisoned

        # flat, non-escalating cooldown: two more exhausting calls must not
        # grow it (old code: 2s -> 4s -> 8s ... -> 60s cap)
        cooldowns = []
        for _ in range(3):
            asyncio.run(r.complete(
                "research_synthesis",
                [{"role": "user", "content": "Reply OK"}]))
            remaining = max(0.0, st.cooldown_until - time.monotonic())
            cooldowns.append(round(remaining, 1))
        assert all(c <= 5.5 for c in cooldowns), cooldowns
