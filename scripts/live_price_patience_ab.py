"""SPEED run 17 — extended A/B: 429 patience-in-place vs early fork failover.

Run 15 set _429_PATIENCE_S=120 with the argument that in-place waiting
strictly dominates sequential patience-then-fork because both tiers queue on
the same upstream. Tonight's first probe FALSIFIED it once: PROXY leg 133.1s
vs FORK leg 16.0s in the same interleaved round. Before touching the policy,
gather a larger paired sample.

Leg PATIENT: shipped post-run-15 router (proxy patience 120s).
Leg EARLYFORK: identical router but task_classes[adversarial_review] pinned
to [ox_alpha_proxy, ox_alpha] with a monkeypatched patience budget of 45s —
i.e. honour one full RA:30 window, decline the second, fork immediately.

Same schema-bearing messages both legs, interleaved P/E/P/E against Portal
drift. Pricing only; no verdict acted upon.
"""
import asyncio, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("OX_ALPHA_PROXY_BASE_URL", "http://127.0.0.1:8645/v1")
os.environ.setdefault("OX_ALPHA_PROXY_API_KEY", "local")

import inference                                            # noqa: E402
from agp.adversary import VERDICT_JSON_SCHEMA               # noqa: E402

MSGS = [
    {"role": "system", "content": "You are the adversary. Return JSON only."},
    {"role": "user",
     "content": 'Conclusion: "test conclusion". Attack it. Return JSON: '
                '{"objections": []} if none.'},
]


async def time_call(router) -> tuple[float, str, bool]:
    t0 = time.monotonic()
    try:
        res = await router.complete(
            "adversarial_review", MSGS,
            schema=VERDICT_JSON_SCHEMA, max_tokens=200, timeout=240)
        return time.monotonic() - t0, res["tier"], True
    except Exception as e:  # noqa: BLE001
        return time.monotonic() - t0, f"ERROR {type(e).__name__}", False


async def main() -> None:
    n_pairs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    patient = inference.ProviderRouter()
    early = inference.ProviderRouter()
    # Early-fork leg: shrink ONLY the patience budget via the module constant
    # the retry loop reads, restored after — isolates the policy variable.
    import inference as inf
    rows = []
    for i in range(n_pairs):
        inf._429_PATIENCE_S = 120.0
        w, tier, ok = await time_call(patient)
        rows.append(("PATIENT", i, w, tier, ok))
        print(f"PATIENT {i}: {w:6.1f}s  tier={tier}  ok={ok}", flush=True)
        inf._429_PATIENCE_S = 45.0
        w, tier, ok = await time_call(early)
        rows.append(("EARLYFORK", i, w, tier, ok))
        print(f"EARLYFORK {i}: {w:6.1f}s  tier={tier}  ok={ok}", flush=True)
    inf._429_PATIENCE_S = 120.0
    for leg in ("PATIENT", "EARLYFORK"):
        ws = [r[2] for r in rows if r[0] == leg]
        oks = [r[4] for r in rows if r[0] == leg]
        print(f"{leg}: n={len(ws)} mean={sum(ws)/len(ws):.1f}s "
              f"min={min(ws):.1f}s max={max(ws):.1f}s ok={sum(oks)}/{len(ws)}")


if __name__ == "__main__":
    asyncio.run(main())
