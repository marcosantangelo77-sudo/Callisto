"""LIVE transport measurement — subprocess vs agent pool, same prompt.

Skipped unless CALLISTO_TRANSPORT_LIVE=1 (needs `hermes portal login` and
network). Run:

    CALLISTO_TRANSPORT_LIVE=1 python3 -m pytest \\
        tests/test_perf_transport_live.py -q -s

Reports the ratio mandated by the perf brief: per-call cost via the fresh
subprocess path vs the warm pool, plus N sequential calls through the pool
to show amortization. Numbers, not adjectives.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.pipeline.transport.agent_pool import AgentPoolTransport  # noqa: E402

PROMPT = [{"role": "user",
           "content": "Reply with exactly: ok"}]

pytestmark = pytest.mark.skipif(
    os.getenv("CALLISTO_TRANSPORT_LIVE") != "1",
    reason="live measurement — set CALLISTO_TRANSPORT_LIVE=1")


def test_subprocess_vs_pool_ratio():
    from tools.pipeline.hermes_cli import hermes_complete

    async def run():
        # 1) subprocess baseline (fresh process per call)
        t0 = time.monotonic()
        res_sub = await hermes_complete(PROMPT, role="perf",
                                        transport="subprocess")
        sub_s = time.monotonic() - t0
        assert res_sub["content"].strip()

        # 2) warm pool: first call includes one-time agent build
        pool = AgentPoolTransport(pool_size=1)
        t0 = time.monotonic()
        res_pool = await pool.complete(PROMPT, role="perf")
        first_s = time.monotonic() - t0
        assert res_pool["content"].strip()

        # 3) amortized: 5 more calls on the warm agent
        t0 = time.monotonic()
        for _ in range(5):
            r = await pool.complete(PROMPT, role="perf")
            assert r["content"].strip()
        warm_mean = (time.monotonic() - t0) / 5

        print(f"\nsubprocess per call : {sub_s:.2f}s")
        print(f"pool first call     : {first_s:.2f}s (includes build)")
        print(f"pool warm mean      : {warm_mean:.2f}s")
        print(f"speedup (warm/sub)  : {sub_s / max(warm_mean, 0.01):.1f}x")
        assert warm_mean < sub_s, (
            "warm pool must beat a fresh subprocess per call")

    asyncio.run(run())


def test_pipeline_question_shape_end_to_end():
    """One realistic multi-call question shape through the shared path."""
    from tools.pipeline.hermes_cli import HermesCliModel

    async def run():
        m = HermesCliModel()
        t0 = time.monotonic()
        results = await asyncio.gather(*[
            m.complete("screening", PROMPT),
            m.complete("classification", PROMPT),
        ])
        elapsed = time.monotonic() - t0
        for r in results:
            assert "content" in r
        print(f"\n2 concurrent pipeline-shaped calls: {elapsed:.2f}s total")
        print("transports used:", [c["transport"] for c in m.calls])
        assert all(c["transport"] == "hermes-agent-pool" for c in m.calls)

    asyncio.run(run())
