"""Run-9 live probe: where does a routed call's time actually go?

Measures, per candidate endpoint in the research_synthesis ladder:
  1. gpu1  (localhost:8080, dead)  — cost of the dead failover attempt
  2. frontier (env unset)          — should be unresolved/skipped
  3. ox_alpha_proxy (live :8646)   — warm call time
Also times a full router.complete() end to end.
No caching, no answer-affecting change; one-word replies.
"""
import asyncio, os, sys, time, json

os.environ["OX_ALPHA_PROXY_BASE_URL"] = "http://127.0.0.1:8646/v1"
os.environ["OX_ALPHA_PROXY_API_KEY"] = "probe"
os.environ["OX_ALPHA_PROXY_MODEL"] = "stealth/ox-alpha"
os.environ.pop("FRONTIER_BASE_URL", None)
os.environ.pop("FRONTIER_API_KEY", None)
os.environ.pop("FRONTIER_MODEL", None)

sys.path.insert(0, "/Users/marcosantangelo/callisto-wt/loop")
from inference import ProviderRouter, _post_with_retry  # noqa: E402

MSG = [{"role": "user", "content": "Reply with exactly one word: ok"}]

async def main():
    r = ProviderRouter()
    out = {}

    # 1. dead gpu1 attempt, raw
    t0 = time.perf_counter()
    try:
        await r._post(r.endpoints["gpu1"], r._payload(
            r.endpoints["gpu1"], MSG, None, None, None), 300.0)
        out["gpu1_raw"] = ("ok", time.perf_counter() - t0)
    except Exception as e:
        out["gpu1_raw"] = (f"{type(e).__name__}", time.perf_counter() - t0)

    # 2. candidates_for on research_synthesis — who is actually in the ladder?
    cands = r.candidates_for("research_synthesis")
    out["candidates"] = cands

    # 3. warm proxy call, raw
    t0 = time.perf_counter()
    try:
        c, u = await r._post(r.endpoints["ox_alpha_proxy"], r._payload(
            r.endpoints["ox_alpha_proxy"], MSG, None, None, None), 120.0)
        out["proxy_raw"] = (c.strip()[:20], round(time.perf_counter() - t0, 2))
    except Exception as e:
        out["proxy_raw"] = (f"{type(e).__name__}: {e}"[:120],
                            round(time.perf_counter() - t0, 2))

    # 4. full router.complete through the ladder
    t0 = time.perf_counter()
    try:
        res = await r.complete("research_synthesis", MSG, timeout=120.0)
        out["router_complete"] = {
            "tier": res["tier"], "wall_s": round(time.perf_counter() - t0, 2),
            "content": res["content"].strip()[:20],
        }
    except Exception as e:
        out["router_complete"] = {"error": str(e)[:200],
                                  "wall_s": round(time.perf_counter() - t0, 2)}

    # 5. second full call (cooldown state now warm — does gpu1 get skipped?)
    t0 = time.perf_counter()
    try:
        res = await r.complete("research_synthesis", MSG, timeout=120.0)
        out["router_complete_2nd"] = {
            "tier": res["tier"], "wall_s": round(time.perf_counter() - t0, 2)}
    except Exception as e:
        out["router_complete_2nd"] = {"error": str(e)[:200],
                                      "wall_s": round(time.perf_counter() - t0, 2)}

    print(json.dumps(out, indent=2))

asyncio.run(main())
