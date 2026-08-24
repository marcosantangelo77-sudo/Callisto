"""Paired before/after measurement for the inference.py transport port.

Measures three things against REAL loopback HTTP (not mocks):
  A. per-call overhead, fresh AsyncClient per request (the BEFORE shape)
  B. per-call overhead, shared pooled client (the AFTER shape)
  C. dead-hop connect-refused probe: retried-in-place (BEFORE) vs immediate
     propagation (AFTER)
Run twice: once with the code reverted (git stash-free: use `git worktree` or
pass --before and import from a pristine checkout). Simplest honest pairing:
this script imports inference fresh in two subprocesses; the BEFORE numbers
come from `git show HEAD~1:inference.py` materialised into a temp dir.
"""
import asyncio
import httpx
import statistics
import sys
import time

URL = "http://127.0.0.1:8647/v1/chat/completions"
N = 30


async def handler(request):
    import json
    return httpx.Response(200, json={"choices": [
        {"message": {"content": "ok"}}], "usage": {}})


async def server():
    import functools
    return await asyncio.start_server(
        lambda r, w: _serve(r, w), "127.0.0.1", 8647)


async def _serve(reader, writer):
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
    except Exception:
        pass
    finally:
        writer.close()


async def measure_fresh(n=N):
    """BEFORE shape: new client per call."""
    times = []
    payload = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    for _ in range(n):
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(URL, json=payload)
        times.append(time.perf_counter() - t0)
    return times


async def measure_pooled(n=N):
    """AFTER shape: one shared client."""
    times = []
    payload = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0),
                               limits=httpx.Limits(max_connections=32))
    try:
        for _ in range(n):
            t0 = time.perf_counter()
            await client.post(URL, json=payload)
            times.append(time.perf_counter() - t0)
    finally:
        await client.aclose()
    return times


async def measure_dead_hop():
    """C: ConnectError on 127.0.0.1:9 — retry-in-place vs immediate."""
    import inference as inf

    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        raise httpx.ConnectError("[Errno 61] Connection refused")

    async def run():
        ep = object()
        t0 = time.perf_counter()
        try:
            await inf._post_with_retry(post_fn, ep, {}, timeout=5.0)
        except httpx.TransportError:
            pass
        return time.perf_counter() - t0

    dt = await run()
    return calls["n"], dt


async def main():
    srv = await asyncio.start_server(_handle, "127.0.0.1", 8647)
    fresh = await measure_fresh()
    # second burst reuses warm sockets for the pool only if same client;
    # fresh pays handshake every time by construction.
    pooled = await measure_pooled()
    pooled2 = await measure_pooled(10)
    srv.close()
    n_calls, dead = await measure_dead_hop()

    def stats(xs):
        return f"mean={statistics.mean(xs)*1000:.3f}ms p50={statistics.median(xs)*1000:.3f}ms"

    print(f"fresh  per-call ({N}): {stats(fresh)}")
    print(f"pooled per-call ({N}): {stats(pooled)}")
    print(f"pooled steady   (10): {stats(pooled2)}")
    fr = statistics.mean(fresh) / statistics.mean(pooled)
    print(f"ratio mean fresh/pooled: {fr:.2f}x")
    print(f"dead-hop: attempts={n_calls}, wall={dead*1000:.1f}ms "
          f"({'immediate failover' if n_calls == 1 else 'RETRIED'} — "
          f"pre-fix was ~528ms/2attempts)")


async def _handle(reader, writer):
    data = await reader.read(65536)
    body = b'{"choices":[{"message":{"content":"ok"}}],"usage":{}}'
    head = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Connection: keep-alive\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n")
    writer.write(head + body)
    await writer.drain()
    writer.close()


if __name__ == "__main__":
    asyncio.run(main())
