"""SPEED RUN 1 — profile the real pipeline end to end, then hold it honest.

Method (findings/speed_2026-08-23.md):
  1. PROFILE FIRST. Drive the five scored retrodiction questions through the
     REAL ResearchPipeline with calibrated seams and record a stage table:
     stage · seconds · model calls · bytes fetched.
     Seam calibration:
       model   0.7s/call  — the POST-FIX transport round trip (11.3s observed
                            minus 10.6s process startup; another instance is
                            landing that fix, so profiles here assume it done)
       fetch   0.3s/call  — median of live measurements this machine
                            (openalex 560ms first-hit, federalregister
                            68–120ms, arxiv 55–358ms); bodies sized ~20KB
       adversary stays its own separate call — never merged (hard rule)
  2. ORACLE. The same five questions at ZERO latency define the answers any
     optimisation must reproduce byte-for-byte (probabilities, stances,
     sealed flags, Brier). A speedup that moves these is a regression.
  3. No caching across cutoffs anywhere here: claim dates are past-dated,
     fixtures are static bytes, nothing leaks post-cutoff evidence.

This file owns measurement only. The bottleneck fix lands separately and
must keep every assertion in this file green.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from tools.pipeline.engine import ResearchPipeline

REPO = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = REPO / "data" / "retro_batch" / "questions.json"
SMOKE_IDS = ("c1672ca2fb", "5b03b4a3a3", "ca8b500dd9", "f5de4cd259",
             "7317d0d47f")  # results_smoke5.jsonl — the five scored questions

MODEL_LATENCY_S = float(__import__("os").environ.get(
    "CALLISTO_SPEED_MODEL_S", "0.7"))
FETCH_LATENCY_S = float(__import__("os").environ.get(
    "CALLISTO_SPEED_FETCH_S", "0.3"))


# ── calibrated seams ───────────────────────────────────────────────────────

def _qid(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class CalibratedModel:
    """Deterministic, latency-bearing stand-in for the post-fix model seam.

    Responses are pure functions of the prompt (same input -> same output),
    so the oracle comparison measures structure, not randomness.
    """

    name = "calibrated-script"

    def __init__(self, latency_s: float = MODEL_LATENCY_S,
                 n_leaves: int = 5):
        self.latency_s = latency_s
        self.n_leaves = n_leaves
        self.calls: list[dict] = []   # {role, wait_s, chars_in}

    async def complete(self, role: str, messages: list[dict],
                       **_ignored) -> dict:
        prompt = "\n".join(m.get("content", "") for m in messages)
        t0 = time.monotonic()
        await asyncio.sleep(self.latency_s)
        waited = time.monotonic() - t0
        self.calls.append({"role": role, "wait_s": round(waited, 3),
                           "chars_in": len(prompt)})
        if role == "Architect":
            return {"content": json.dumps(
                {"sub_questions": [
                    {"text": f"{_leaves(prompt)} angle {i}",
                     "kind": "descriptive", "question_type":
                     f"angle {i} evidence",
                     "min_source_tier": 2, "min_independent_sources": 2,
                     "quant_required": False, "horizon_days": None}
                    for i in range(1, self.n_leaves + 1)]})}
        if role == "Manager":
            h = int(_qid(prompt), 16)
            return {"content": json.dumps(
                {"answer": f"synthesis for prompt-{h % 97}",
                 "proposed_confidence": 0.55 + (h % 10) / 100,
                 "stance": "AFFIRMS" if h % 3 else "DENIES",
                 "compute": None})}
        raise AssertionError(f"unexpected role {role}")


def _leaves(prompt: str) -> str:
    """Root question text from the decompose prompt (after 'QUESTION: ')."""
    return prompt.split("QUESTION:")[-1].strip().splitlines()[0][:120]


class MeasuredTransport:
    """Fixture transport with realistic blocking latency and byte counting.

    Deliberately BLOCKING (time.sleep, like urllib today): the sync-I/O
    reality of the current retrieval path is part of what we measure.
    Serves OpenAlex-shaped bodies embedding the URL's own search terms so
    the relevance gate admits round-1 results — the control-flow shape of
    the observed live run (fan-out, sufficiency, stop).
    """

    def __init__(self, latency_s: float = FETCH_LATENCY_S,
                 body_bytes: int = 20_000):
        self.latency_s = latency_s
        self.body_bytes = body_bytes
        self.calls = 0
        self.bytes_out = 0
        self.blocked_s = 0.0
        self.urls: list[str] = []

    def __call__(self, url: str, headers: dict) -> tuple[int, str]:
        t0 = time.monotonic()
        time.sleep(self.latency_s)
        self.blocked_s += time.monotonic() - t0
        self.calls += 1
        self.urls.append(url)
        term = _search_term(url)
        filler = "x" * max(64, self.body_bytes)
        body = json.dumps({
            "meta": {"query": term, "count": 3},
            "results": [
                {"title": f"{term} study {i}",
                 "display_name": f"{term} evidence {i}",
                 "abstract": f"research about {term} finding {i} "
                             f"{filler[:64]}"}
                for i in range(3)],
            "_filler": filler,
        })
        self.bytes_out += len(body)
        return 200, body


def _search_term(url: str) -> str:
    import urllib.parse
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("search", "query.term", "term", "query_term", "q"):
        if key in q:
            return q[key][0][:80]
    tail = url.rsplit("/", 1)[-1]
    return (tail or "research")[:80]


class QuietAdversary:
    """Separate adversarial call — NEVER merged into the author's context.
    Returns no objections so the run seals; still costs its model call."""

    name = "quiet-adversary"

    def __init__(self):
        self.calls = 0

    async def complete(self, task_class, messages, schema=None):
        await asyncio.sleep(MODEL_LATENCY_S)
        self.calls += 1
        return {"parsed_json": {"objections": []},
                "model": self.name}


# ── instrumentation ────────────────────────────────────────────────────────

def _wrap_async(obj, name, log: list, label: str):
    orig = getattr(obj, name)

    async def wrapped(*a, **k):
        t0 = time.monotonic()
        try:
            return await orig(*a, **k)
        finally:
            log.append((label, time.monotonic() - t0))

    setattr(obj, name, wrapped)


async def run_one(question: str, *, claim_date=None, model_latency=None,
                  fetch_latency=None, collect: bool = False):
    """One question through the real pipeline, instrumented. Returns
    (result, profile_dict)."""
    from datetime import date as _date
    model = CalibratedModel(latency_s=(MODEL_LATENCY_S if model_latency
                                       is None else model_latency))
    transport = MeasuredTransport(latency_s=(FETCH_LATENCY_S if fetch_latency
                                             is None else fetch_latency))
    adv = QuietAdversary()
    pipe = ResearchPipeline(model=model, adversary_router=adv,
                            transport=transport)
    stages: list = []
    _wrap_async(pipe, "_decompose", stages, "decompose")
    _wrap_async(pipe, "_fetch_for_question", stages, "fetch_leaf")
    _wrap_async(pipe, "_answer_leaf", stages, "answer_leaf")
    _wrap_async(pipe.adversary, "attack", stages, "adversary")
    t0 = time.monotonic()
    result = await pipe.run(question, claim_date=claim_date or _date(2024, 1, 3))
    wall = time.monotonic() - t0

    agg: dict[str, dict] = {}
    for label, dt in stages:
        s = agg.setdefault(label, {"n": 0, "s": 0.0})
        s["n"] += 1
        s["s"] += dt
    model_by_role: dict[str, dict] = {}
    for c in model.calls:
        r = model_by_role.setdefault(c["role"], {"n": 0, "s": 0.0})
        r["n"] += 1
        r["s"] += c["wait_s"]
    profile = {
        "question": question[:60],
        "wall_s": round(wall, 3),
        "stages": {k: {"n": v["n"], "s": round(v["s"], 3)}
                   for k, v in agg.items()},
        "model_calls": {k: {"n": v["n"], "s": round(v["s"], 3)}
                        for k, v in model_by_role.items()},
        "model_calls_total": len(model.calls),
        "fetch_calls": transport.calls,
        "bytes_fetched": transport.bytes_out,
        "fetch_blocked_s": round(transport.blocked_s, 3),
        "adversary_calls": adv.calls,
        "sealed": result.sealed,
        "refusal_reason": result.refusal_reason,
        "confidence": result.confidence_score,
        "stance": result.stance,
    }
    if collect:
        pred = _prediction_of(result, question)
        return result, profile, pred
    return result, profile


def _prediction_of(result, question: str):
    """The retro.PipelineResearcher probability mapping, verbatim."""
    conf = result.confidence_score if result.sealed else 0.0
    stance = getattr(result, "stance", "UNDETERMINED")
    if stance == "AFFIRMS":
        prob = 0.5 + conf / 2.0
    elif stance == "DENIES":
        prob = 0.5 - conf / 2.0
    else:
        prob = 0.5
    return {"question": _qid(question), "probability": prob,
            "sealed": result.sealed, "stance": result.stance,
            "confidence": result.confidence_score}


def _five_questions() -> list[dict]:
    qs = json.loads(QUESTIONS_PATH.read_text())
    out = [q for q in qs if any(q["question_id"].startswith(s)
                                for s in SMOKE_IDS)]
    assert len(out) == 5, f"expected the five scored questions, got {len(out)}"
    return out


# ── the profile ────────────────────────────────────────────────────────────

def test_profile_end_to_end_stage_table(capsys):
    """STAGE TABLE — the before-measurement this run acts on."""
    questions = _five_questions()
    rows = []
    for q in questions:
        _, prof = _loop().run_until_complete(
            run_one(q["text"], claim_date=q["claim_date"]))
        rows.append(prof)

    def tot(key):
        return round(sum(r[key] for r in rows), 2)

    stage_tot = {}
    for r in rows:
        for k, v in r["stages"].items():
            s = stage_tot.setdefault(k, {"n": 0, "s": 0.0})
            s["n"] += v["n"]
            s["s"] += v["s"]

    print("\n===== SPEED PROFILE (calibrated seams: model %.2fs, fetch %.2fs)"
          % (MODEL_LATENCY_S, FETCH_LATENCY_S))
    print(f"{'stage':<16}{'calls':>7}{'seconds':>10}")
    for k, v in sorted(stage_tot.items(), key=lambda kv: -kv[1]["s"]):
        print(f"{k:<16}{v['n']:>7}{v['s']:>10.2f}")
    unattributed = tot("wall_s") - sum(v["s"] for v in stage_tot.values())
    print(f"{'other/join':<16}{'':>7}{unattributed:>10.2f}")
    print(f"\nfive questions total: {tot('wall_s')}s  "
          f"model calls: {sum(r['model_calls_total'] for r in rows)}  "
          f"fetches: {tot('fetch_calls')}  "
          f"bytes: {tot('bytes_fetched')}")
    print("\nper-question walls: " +
          ", ".join(f"{r['wall_s']}s" for r in rows))
    print("=====")

    # structural invariants: the profile reflects a real, sealing run
    assert all(r["sealed"] for r in rows), \
        [r["refusal_reason"] for r in rows]
    assert sum(r["model_calls_total"] for r in rows) == 5 * (1 + 5 + 1), \
        "decompose + one Manager per leaf + one adversary attack per question"
    assert tot("fetch_calls") > 0


def test_answers_identical_under_latency():
    """ORACLE: zero-latency and calibrated-latency runs agree byte-for-byte.

    Any future speedup must keep this true — a faster run that shifts an
    answer is a regression (standing-role rule 4)."""
    questions = _five_questions()

    zero = []
    calib = []
    for q in questions:
        _, _, p0 = _loop().run_until_complete(run_one(
            q["text"], claim_date=q["claim_date"],
            model_latency=0.0, fetch_latency=0.0, collect=True))
        zero.append(p0)
        _, _, p1 = _loop().run_until_complete(run_one(
            q["text"], claim_date=q["claim_date"], collect=True))
        calib.append(p1)
    assert zero == calib


def _loop():
    import asyncio
    return asyncio.get_event_loop()
