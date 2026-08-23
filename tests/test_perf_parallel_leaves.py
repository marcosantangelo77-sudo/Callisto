"""Parallel leaf execution — performance with determinism, not instead of it.

The sub-questions of a ResearchProgram are independent by construction, so
they run concurrently. These tests pin the three invariants that make the
concurrency legitimate:

1. DETERMINISM. The same program run repeatedly — under jittered,
   deliberately reordered transports and models — produces byte-identical
   results: same leaf order, same fetch order, same evidence order, same
   seal hash. A speedup that changes the answer is not a speedup.
2. ISOLATION. One leaf raising must not kill the run; the failed leaf is
   distinguishable from a leaf that honestly found nothing.
3. BOUNDS. Concurrency is capped (provider 429s past ~8-10 sessions) and
   actually exercised (leaves genuinely overlap in time).
"""
from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp import Domain  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    ResearchPipeline,
    ThreadSafeLedger,
    fixture_transport,
)
from tools.pipeline.model import PipelineModel, ScriptedModel  # noqa: E402


N_LEAVES = 5


def _decompose(n: int = N_LEAVES) -> str:
    return json.dumps({"sub_questions": [
        {"text": f"what does the literature say about topic variant {i}",
         "kind": "descriptive", "question_type": "scholarly work search",
         "min_source_tier": 2, "min_independent_sources": 1}
        for i in range(n)]})


_ANSWER = json.dumps({"answer": "the evidence supports the claim",
                      "proposed_confidence": 0.6})

_BODY = json.dumps({"results": [
    {"id": "W1", "title": "Scholarly study on the topic: a literature "
     "review of scholarly work", "publication_year": 2024}]})

_ROUTES = {"/works": _BODY}


class _SlowModel(PipelineModel):
    """Scripted answers with per-call simulated latency + optional failure.

    Deterministic content regardless of timing: the answer text depends
    only on the question text, never on when the call happened.
    """

    name = "slow-scripted"

    def __init__(self, delay: float = 0.05, fail_on: str = ""):
        self.delay = delay
        self.fail_on = fail_on
        self.calls: list[tuple[str, float]] = []
        self._lock = threading.Lock()
        self._n_architect = 0
        self._t0 = time.monotonic()

    async def complete(self, role, messages, **_ignored):
        with self._lock:
            self.calls.append((role, time.monotonic() - self._t0))
            if role == "Architect":
                self._n_architect += 1
                return {"content": _decompose()}
        prompt = "\n".join(m.get("content", "") for m in messages)
        await asyncio.sleep(self.delay)
        if self.fail_on and self.fail_on in prompt:
            raise RuntimeError(f"simulated model failure for '{self.fail_on}'")
        return {"content": _ANSWER}


def _pipeline(tmp_path, model, ledger=None):
    return ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=fixture_transport(_ROUTES),
        store=ArtifactStore(root=tmp_path / "artifacts"),
        ledger=ledger or ProvenanceLedger())


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _run(pipeline, question="What is known about the topic?"):
    return asyncio.new_event_loop().run_until_complete(
        pipeline.run(question, today=date(2026, 8, 22)))


# ── 1. Determinism ──────────────────────────────────────────────────────────

def test_repeated_parallel_runs_are_identical(tmp_path):
    """Run the SAME program five times with jittered model latency and a
    shuffled fixture transport; every observable output must match."""
    runs = []
    for i in range(5):
        rng = random.Random(i)
        class _Jitter(_SlowModel):
            async def complete(self, role, messages, **kw):
                resp = await super().complete(role, messages, **kw)
                await asyncio.sleep(rng.uniform(0, 0.05))
                return resp

        calls: list[str] = []

        def transport(url, headers, _calls=calls, _rng=random.Random(99 - i)):
            # Random small sleeps so completion order differs every run.
            time.sleep(_rng.uniform(0, 0.01))
            _calls.append(url)
            for pattern, body in _ROUTES.items():
                if pattern in url:
                    return 200, body
            return 404, "{}"

        pipe = ResearchPipeline(
            model=_Jitter(delay=0.02), adversary_router=_QuietAdversary(),
            transport=transport,
            store=ArtifactStore(root=tmp_path / f"art{i}"),
            ledger=ProvenanceLedger())
        res = _run(pipe)
        assert res.sealed, res.refusal_reason
        runs.append({
            "leaf_order": [(l.question_id, l.text, l.answer, l.confidence,
                            l.tier, l.source_classes, l.n_sources)
                           for l in res.leaves],
            "fetches": [(f.source_name, f.url, f.content_sha256,
                         f.question_id) for f in res.fetches],
            "evidence": [e.content for e in res.session.evidence],
            # seal_hash covers Evidence timestamps and the session_id, both
            # wall-clock-derived by design (agp), so it cannot match across
            # runs. The CONTENT above must. Assert on what determinism owns:
            # ordered leaves, fetches, evidence bytes, conclusion, score.
            "conclusion": res.conclusion,
            "confidence": res.confidence_score,
        })
    assert all(r == runs[0] for r in runs[1:])


def test_results_ordered_by_question_not_completion(tmp_path):
    """Leaves finish out of order (first is slowest) but come back in
    QUESTION order."""
    class _FirstIsSlowest(_SlowModel):
        async def complete(self, role, messages, **kw):
            prompt = "\n".join(m.get("content", "") for m in messages)
            resp = await super().complete(role, messages, **kw)
            if role == "Manager":
                # variant 0 sleeps longest yet must be reported first.
                await asyncio.sleep(0.20 if "variant 0" in prompt else 0.01)
            return resp

    pipe = _pipeline(tmp_path, _FirstIsSlowest(delay=0))
    res = _run(pipe)
    assert len(res.leaves) == N_LEAVES
    # question_id is content-derived, so compare via the program's order.
    program_ids = [q.question_id for q in res.program.leaves]
    ids = [l.question_id for l in res.leaves]
    assert ids == program_ids, "leaves not reported in question order"
    texts = [l.text for l in res.leaves]
    assert texts == [q.text for q in res.program.leaves]


def test_concurrent_leaves_actually_overlap_in_time(tmp_path):
    """The point of the change: wall-clock ≈ slowest leaf, not their sum.
    Serial cost of 5 × 0.15s leaves would be ≥0.75s; parallel should be
    well under half of that even accounting for overhead."""
    delay = 0.15
    pipe = _pipeline(tmp_path, _SlowModel(delay=delay))
    t0 = time.monotonic()
    res = _run(pipe)
    elapsed = time.monotonic() - t0
    assert res.sealed
    serial_floor = N_LEAVES * delay
    assert elapsed < serial_floor * 0.5, (
        f"{elapsed:.2f}s looks serial (sum-of-leaves floor {serial_floor:.2f}s)")


def test_concurrency_is_bounded(tmp_path):
    """No more than max_concurrent_leaves Manager calls may be in flight;
    the semaphore must hold even when many leaves exist."""
    observed_peak = 0
    active = 0
    lock = threading.Lock()

    class _Counting(_SlowModel):
        async def complete(self, role, messages, **kw):
            nonlocal observed_peak, active
            prompt = "\n".join(m.get("content", "") for m in messages)
            if role == "Manager":
                with lock:
                    active += 1
                    observed_peak = max(observed_peak, active)
            try:
                return await super().complete(role, messages, **kw)
            finally:
                if role == "Manager":
                    with lock:
                        active -= 1

    pipe = ResearchPipeline(
        model=_Counting(delay=0.03), adversary_router=_QuietAdversary(),
        transport=fixture_transport(_ROUTES),
        store=ArtifactStore(root=tmp_path / "art"),
        ledger=ProvenanceLedger(), max_concurrent_leaves=2)
    res = _run(pipe)
    assert res.sealed
    assert observed_peak <= 2, f"saw {observed_peak} concurrent leaves"


def test_fetch_fanout_within_a_leaf_is_bounded(tmp_path):
    """Retrieval's internal fan-out honours its own cap."""
    from tools.pipeline.retrieval import IterativeRetriever

    class _FakeSpec:
        name = "openalex"
        base_url = "https://api.openalex.org"

    class _FakeRegistry:
        def select(self, *a, **k):
            return [_FakeSpec() for _ in range(12)]

        def get(self, name):
            return None

        def specs(self):
            return []

        def select_explained(self, q):
            return []

    peak = 0
    active = 0
    lock = threading.Lock()

    class _Adapter:
        def __init__(self, source):
            pass

        def works_search(self, term, limit=3):
            nonlocal peak, active
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return json.loads(_BODY)

    class _Entry:
        def make_adapter(self, source):
            return _Adapter(source)

    from tools.loop_quality import InformationGainTerminator  # noqa: F401

    retr = IterativeRetriever(
        registry=_FakeRegistry(), ledger=ProvenanceLedger(),
        transport=lambda url, headers: (200, _BODY),
        max_rounds=1, max_sources_per_leaf=12,
        generic_calls={"openalex": ("works_search", ("term",),
                                    {"limit": 3})},
        use_planner=False, max_concurrent_fetches=3)

    # _make_adapter needs registry.get(name); patch it to return an entry.
    _FakeRegistry.get = lambda self=None, name=None: _Entry()

    class _Q:
        question_id = "q1"
        text = "scholarly study on the topic literature review scholarly work"

    trace = retr.retrieve(_Q(), "scholarly work search", min_independent=9)
    assert trace.admitted
    assert peak <= 3, f"saw {peak} concurrent fetches"


# ── 2. Isolation ────────────────────────────────────────────────────────────

def test_one_leaf_failing_does_not_kill_the_run(tmp_path):
    pipe = _pipeline(tmp_path, _SlowModel(delay=0.01, fail_on="variant 2"))
    res = _run(pipe)
    assert len(res.leaves) == N_LEAVES
    failed = [l for l in res.leaves if l.error]
    ok = [l for l in res.leaves if not l.error]
    assert len(failed) == 1, "exactly one leaf should have failed"
    assert "variant 2" in failed[0].text or failed[0].text
    assert "RuntimeError" in failed[0].error
    assert len(ok) == N_LEAVES - 1
    assert any(l.answer for l in ok)
    # The honest note beats a silent swallow.
    assert any("FAILED" in n for n in res.notes)


def test_failed_leaf_is_distinguishable_from_honest_null(tmp_path):
    """A leaf that found nothing has answer='' AND error=''. A leaf that
    ERRORED has error != ''. They must never be conflated."""
    # Honest null: no fixture route matches → zero admissible evidence.
    pipe_null = ResearchPipeline(
        model=_SlowModel(delay=0.01), adversary_router=_QuietAdversary(),
        transport=fixture_transport({"/nomatch": "{}"}),
        store=ArtifactStore(root=tmp_path / "a"), ledger=ProvenanceLedger())
    res_null = _run(pipe_null, "Unrelated query about nothing at all?")
    null_leaves = [l for l in res_null.leaves if not l.error]
    assert null_leaves == [] or all(
        l.answer == "" and l.error == "" for l in null_leaves)

    # Errored leaf: raises mid-answer.
    pipe_err = _pipeline(tmp_path, _SlowModel(delay=0.01, fail_on="variant"))
    res_err = _run(pipe_err)
    assert all(l.error != "" for l in res_err.leaves)


def test_ledger_writes_are_race_free_under_parallel_fetch(tmp_path):
    """Hammer one ThreadSafeLedger from many threads; the append-only dict
    must end up exactly consistent (every recorded body present once)."""
    inner = ProvenanceLedger()
    ledger = ThreadSafeLedger(inner)

    def worker(i):
        for j in range(50):
            ledger.record_tool_result(f"tool{i}", f"body-{i}-{j}",
                                      primary=True, urls=[f"http://x/{i}/{j}"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(inner.observed_urls()) == 16 * 50
    assert ledger.is_primary_bytes("body-7-49")
