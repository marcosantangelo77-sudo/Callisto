"""Run-18 A/B: answer-stage prompt size vs wall latency (live, same model).

Interleaved order controls capacity drift. Same completion task, only the
evidence payload differs (1x4000-char body vs 5x4000). No caching; nothing
crosses a cutoff.
"""
import asyncio, time, os, sys
os.environ.setdefault('OX_ALPHA_PROXY_BASE_URL', 'http://127.0.0.1:8646/v1')
os.environ.setdefault('OX_ALPHA_PROXY_API_KEY', 'local')
sys.path.insert(0, '.')
import inference, json

d = json.load(open('/tmp/oa3.json'))
body = json.dumps(d, sort_keys=True)
q = "semiconductor supply chain resilience"
small = [{"role": "user", "content":
          f"QUESTION: {q}\nEVIDENCE:\n- [0] {body[:4000]}"}]
big = [{"role": "user", "content":
        f"QUESTION: {q}\nEVIDENCE:\n" +
        "\n".join(f"- [{i}] {body[:4000]}" for i in range(5))}]
print('small bytes', len(small[0]['content']),
      'big bytes', len(big[0]['content']))
r = inference.ProviderRouter()


async def go():
    for name, msgs in (('S1', small), ('B1', big), ('S2', small),
                       ('B2', big), ('S3', small), ('B3', big)):
        t = time.monotonic()
        res = await r.complete('research_synthesis', msgs,
                               max_tokens=300, timeout=180)
        print(name, round(time.monotonic() - t, 1), 's')

asyncio.run(go())
