"""SPEED run 18 — answer-stage concurrency probe.

Phase B gathers 5 leaf answers concurrently, but the live e2e profile showed
near-serial service (busy/span ratio 1.25). This probe isolates the cause:
issue N concurrent routed calls on MANAGER's task class and print each call's
start offset and duration. If starts cluster at t=0, concurrency is real and
the serialization lives upstream (Portal queueing). If starts stagger, the
serialization is local (semaphore / event loop).
"""
import asyncio, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OX_ALPHA_PROXY_BASE_URL", "http://127.0.0.1:8645/v1")
os.environ.setdefault("OX_ALPHA_PROXY_API_KEY", "local")
os.environ.setdefault("CALLISTO_STATE_DIR", "/tmp/callisto_speed_state")

import inference                                                    # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
TASK = sys.argv[2] if len(sys.argv) > 2 else "extraction"
MSGS = [{"role": "user", "content": "Reply with the single word: ok"}]

async def one(router, i, t0):
    s = time.monotonic()
    try:
        r = await router.complete(TASK, MSGS)
        print(f"call {i}: start=+{s-t0:6.2f}s dur={time.monotonic()-s:6.2f}s "
              f"tier={r['tier']}", flush=True)
    except Exception as e:
        print(f"call {i}: start=+{s-t0:6.2f}s FAIL {type(e).__name__}: "
              f"{str(e)[:80]}", flush=True)

async def main():
    router = inference.ProviderRouter()
    # warm the dead-hop cooldown so it doesn't pollute offsets
    await router.complete(TASK, MSGS)
    t0 = time.monotonic()
    print(f"firing {N} concurrent {TASK} calls", flush=True)
    await asyncio.gather(*(one(router, i, t0) for i in range(N)))
    print(f"total span {time.monotonic()-t0:.2f}s")

if __name__ == "__main__":
    asyncio.run(main())
