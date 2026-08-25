"""SPEED run 18 — live end-to-end profile: one real question through the
whole pipeline on the live Portal (via ox_alpha proxy), stage-timed.

Read-only measurement harness. Writes nothing to the repo; run record goes
to stdout as a stage table.
"""
import asyncio, json, os, sys, time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OX_ALPHA_PROXY_BASE_URL", "http://127.0.0.1:8645/v1")
os.environ.setdefault("OX_ALPHA_PROXY_API_KEY", "local")
os.environ.setdefault("CALLISTO_STATE_DIR", "/tmp/callisto_speed_state")

import inference                                                    # noqa: E402
from tools.pipeline.engine import ResearchPipeline                  # noqa: E402
from tools.pipeline.model import RouterModel                        # noqa: E402

QUESTION = ("Will the Federal Reserve cut its policy rate at its next "
            "meeting? Give a probability.")

STAGES = []  # (label, t0, t1, role/task_class)

class TimedRouter:
    """Wraps ProviderRouter, recording per-call wall time + task class."""
    def __init__(self, inner):
        self.inner = inner
        self.calls = []
    async def complete(self, task_class, messages, **kw):
        t0 = time.monotonic()
        try:
            return await self.inner.complete(task_class, messages, **kw)
        finally:
            self.calls.append((task_class, t0, time.monotonic(),
                               sum(len(m.get("content") or "") for m in messages)))
    def __getattr__(self, name):
        return getattr(self.inner, name)


async def main():
    router = TimedRouter(inference.ProviderRouter())
    model = RouterModel(router)

    pipe = ResearchPipeline(model=model)   # adversary falls back to model (self-review, capped) — pricing only
    t0 = time.monotonic()
    result = await pipe.run(QUESTION) if hasattr(pipe, "run") else None
    if result is None:
        raise SystemExit(f"no run entrypoint; attrs={[a for a in dir(pipe) if not a.startswith('_')]}")
    wall = time.monotonic() - t0

    print(f"\nquestion: {QUESTION[:70]}...")
    print(f"sealed={result.sealed} leaves={len(result.leaves)} "
          f"conf={getattr(result.session.summary, 'confidence_score', None) if result.session else None}")
    print(f"refusal: {result.refusal_reason}")
    print(f"\nwall: {wall:.2f}s")
    by_class = Counter()
    for tc, t0c, t1c, bytes_in in router.calls:
        by_class[tc] += (t1c - t0c)
        print(f"  call {tc:24s} {t1c-t0c:6.2f}s  prompt~{bytes_in}B")
    print("busy per task_class:", dict(by_class))
    n_fetch = sum(len(l.source_classes) for l in result.leaves)
    print("fetches admitted:", n_fetch,
          "bytes:", sum(len(e.content) for e in result.session.evidence) if result.session else 0)


if __name__ == "__main__":
    asyncio.run(main())
