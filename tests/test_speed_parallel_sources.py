"""SPEED run 2 — parallel per-source fan-out inside one retrieval round.

findings/speed_2026-08-23.md (run 2 section): after the parallel-leaves
restructure, the next measured cost was the strictly serial `for spec in
specs:` fetch loop in IterativeRetriever.retrieve(). These tests pin:

1. ANSWERS DID NOT CHANGE — the existing golden fingerprints and Brier
   regression (tests/test_speed_parallel_leaves.py) cover this; here we pin
   per-source ORDER of admitted fetches and ledger replay order under
   concurrency.
2. THE SPEEDUP IS REAL — with simulated per-fetch latency, a round fanning
   out over N sources must cost far less than N × one source, and must show
   concurrent in-flight fetches.
"""
from __future__ import annotations

import asyncio
import threading
import time

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.pipeline.retrieval import IterativeRetriever  # noqa: E402
from tools.sources.registry import SourceRegistry, SourceAdapter  # noqa: E402
from tools.sources.base import SourceSpec  # noqa: E402

from tools.pipeline.engine import fixture_transport  # noqa: E402


def _make_adapter(source):
    class _Ad:
        def __getattr__(self, method_name):
            def call(*args, **kwargs):
                url = source.spec.base_url + "/works"
                return source.get_json(url)[0]
            return call
    return _Ad()

BODY = ('{"results": [{"id": "W1", "title": "Scholarly study on apple '
        'earnings expectations: analyst consensus and quarterly results",'
        ' "publication_year": 2024, "cited_by_count": 12}]}')


def _registry(n):
    reg = SourceRegistry()
    for i in range(n):
        name = f"slowsrc{i}"
        spec = SourceSpec(
            name=name, base_url=f"https://slow{i}.example",
            description="apple earnings expectations analyst consensus",
            answers=("apple earnings expectations analyst consensus "
                     "quarterly results",),
            cannot_answer=("x",), tier=1, min_interval_s=0.0)
        reg.register(SourceAdapter(spec=spec, make_adapter=_make_adapter))
    return reg


def _question():
    from agp.research_program import (EvidenceRequirement, QuestionKind,
                                      SourceClassRank, ResearchQuestion)
    rq = ResearchQuestion(
        text="what does the evidence say about apple earnings expectations "
             "and analyst consensus",
        kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY, min_independent_sources=1)
    return rq


class OverlapTransport:
    """fixture transport + per-host blocking delay + concurrency probe."""

    def __init__(self, n_hosts, delay_s, reverse=False):
        self.inner = fixture_transport({f"/works": BODY})
        self.delay_s = delay_s
        self.reverse = reverse
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def __call__(self, url, headers):
        delay = self.delay_s
        if self.reverse:
            # later-numbered hosts finish FIRST
            for i in range(10):
                if f"slow{i}.example" in url:
                    delay = self.delay_s * (10 - i)
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(delay)
            return self.inner(url, headers)
        finally:
            with self._lock:
                self.in_flight -= 1


def _retrieve(reg, transport, ledger, n=10):
    ret = IterativeRetriever(
        registry=reg, ledger=ledger, transport=transport, max_rounds=1,
        max_sources_per_leaf=n,
        use_planner=False,
        generic_calls={f"slowsrc{i}": ("works_search", ("term",), {})
                       for i in range(10)})
    return ret.retrieve(_question(), "", 1)


def test_admitted_fetches_stay_in_spec_order_under_concurrency(tmp_path):
    """Reverse the delays so completion order is REVERSE of spec order; the
    admitted list and ledger state must still come back in spec order."""
    tr = OverlapTransport(4, 0.03, reverse=True)
    trace = _retrieve(_registry(4), tr, ProvenanceLedger(), n=4)
    assert tr.max_in_flight >= 3, (
        f"sources did not overlap (max in flight {tr.max_in_flight})")
    srcs = [f.source_name for f in trace.admitted]
    assert srcs == sorted(srcs), (
        f"admitted fetches not in spec order under concurrency: {srcs}")
    assert len(srcs) == 4, srcs


def test_ledger_replay_order_deterministic_under_concurrency():
    """Two identical runs with adversarial timing must produce identical
    primary-ledger hash sets and per-key insertion order."""
    fps = []
    for _ in range(2):
        led = ProvenanceLedger()
        tr = OverlapTransport(5, 0.02, reverse=True)
        trace = _retrieve(_registry(5), tr, led, n=5)
        fps.append((sorted(trace.independent_keys),
                    sorted(h for h, obs in led._by_hash.items()
                           if any(o.primary for o in obs)),
                    [f.content_sha256 for f in trace.admitted]))
    assert fps[0] == fps[1]


def test_round_fetch_wall_is_sublinear_in_source_count():
    """5 slowed sources ≈ one fetch, not five — proof of real overlap."""
    def timed(n):
        tr = OverlapTransport(n, 0.05)
        t0 = time.monotonic()
        trace = _retrieve(_registry(n), tr, ProvenanceLedger(), n=n)
        return time.monotonic() - t0, trace, tr

    t5, trace5, tr5 = timed(5)
    t1, _, _ = timed(1)
    assert trace5.n_admitted == 5
    assert tr5.max_in_flight == 5
    # Serial would be 5×0.05=0.25s; overlapped ≈0.05s. Generous headroom for
    # CI noise but tight enough to catch re-serialization.
    assert t5 < t1 * 3.0 + 0.10, (
        f"5-source round took {t5:.3f}s vs 1-source {t1:.3f}s — fan-out "
        "looks serial")
