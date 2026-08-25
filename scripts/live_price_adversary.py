"""LIVE pricing of the run-16 adversary fix, this machine.

Leg PROXY: the shipped post-run-16 path — schema-bearing adversarial_review
call through ProviderRouter.complete(); ox_alpha_proxy is now admissible and
serves over persistent HTTP.

Leg FORK: the exact pre-run-16 path — a router configured so ONLY ox_alpha
(hermes_cli fresh fork) can serve the same schema-bearing call, reproducing
what every adversary call paid before this run.

Same messages, same VERDICT_JSON_SCHEMA, same timeout policy; legs are
interleaved A/B/A/B/A/B against Portal capacity drift. No caching, no
cutoff involvement, no verdict is acted upon — pricing only.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("OX_ALPHA_PROXY_BASE_URL", "http://127.0.0.1:8645/v1")
os.environ.setdefault("OX_ALPHA_PROXY_API_KEY", "local")

import inference  # noqa: E402
from agp.adversary import VERDICT_JSON_SCHEMA  # noqa: E402

MSGS = [
    {"role": "system",
     "content": "You are the adversary. Return JSON only."},
    {"role": "user",
     "content": 'Conclusion: "test conclusion". Attack it. Return JSON: '
                '{"objections": []} if none.'},
]


async def time_call(router) -> tuple[float, str, bool]:
    t0 = time.monotonic()
    try:
        res = await router.complete(
            "adversarial_review", MSGS,
            schema=VERDICT_JSON_SCHEMA, max_tokens=200, timeout=180)
        return time.monotonic() - t0, res["tier"], True
    except Exception as e:  # noqa: BLE001 — pricing records failures too
        return time.monotonic() - t0, f"ERROR {type(e).__name__}", False


async def main() -> None:
    proxy_router = inference.ProviderRouter()  # post-run-16 admission

    fork_router = inference.ProviderRouter()
    fork_router.task_classes["adversarial_review"] = ["ox_alpha"]

    print("proxy cands:", proxy_router.candidates_for(
        "adversarial_review", schema=VERDICT_JSON_SCHEMA))
    print("fork  cands:", fork_router.candidates_for(
        "adversarial_review", schema=VERDICT_JSON_SCHEMA))

    rows = []
    for i in range(3):
        w, tier, ok = await time_call(proxy_router)
        rows.append(("PROXY", i, w, tier, ok))
        print(f"PROXY {i}: {w:6.1f}s  tier={tier}  ok={ok}")
        w, tier, ok = await time_call(fork_router)
        rows.append(("FORK", i, w, tier, ok))
        print(f"FORK  {i}: {w:6.1f}s  tier={tier}  ok={ok}")

    for leg in ("PROXY", "FORK"):
        ws = [r[2] for r in rows if r[0] == leg]
        oks = [r[4] for r in rows if r[0] == leg]
        print(f"{leg}: n={len(ws)} mean={sum(ws)/len(ws):.1f}s "
              f"min={min(ws):.1f}s max={max(ws):.1f}s ok={sum(oks)}/{len(ws)}")


if __name__ == "__main__":
    asyncio.run(main())
