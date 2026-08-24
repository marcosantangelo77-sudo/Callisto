"""LIVE pricing of the routed path, this machine, run 10.

Sends the same one-word prompt through ProviderRouter.complete() exactly as
the pipeline would, with OX_ALPHA_PROXY_BASE_URL pointed at the running
`hermes proxy` on 127.0.0.1:8646. Records which tier served each call and
the wall time. No caching, no cutoff involvement, adversary untouched.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("OX_ALPHA_PROXY_BASE_URL", "http://127.0.0.1:8646/v1")
os.environ.setdefault("OX_ALPHA_PROXY_API_KEY", "local")

import inference  # noqa: E402

PROMPT = "Reply with exactly: OK"


async def main() -> None:
    router = inference.ProviderRouter()
    print("candidates research_synthesis:", router.candidates_for("research_synthesis"))
    walls = []
    for i in range(4):
        t0 = time.monotonic()
        try:
            res = await router.complete(
                "research_synthesis",
                [{"role": "user", "content": PROMPT}],
                max_tokens=200, timeout=120)
            wall = time.monotonic() - t0
            walls.append(wall)
            served = res["tier"]
            content = (res["content"] or "")[:20].replace("\n", " ")
            print(f"call {i}: {wall:6.1f}s  tier={served}  out={content!r}")
        except Exception as e:  # noqa: BLE001
            wall = time.monotonic() - t0
            walls.append(wall)
            print(f"call {i}: {wall:6.1f}s  ERROR {type(e).__name__}: {str(e)[:120]}")
    if walls:
        print(f"\nmean {sum(walls)/len(walls):.1f}s  total {sum(walls):.1f}s over {len(walls)} calls")


if __name__ == "__main__":
    asyncio.run(main())
