"""Standing-speed role: instrumented end-to-end pipeline profile.

Runs one real question through ResearchPipeline offline (fixture transport,
scripted responses) while recording an exact schedule of every model call
and every fetch: start offset, duration, bytes. The simulated per-call
latencies are parameters so the SAME structural profile can be priced under
different transport assumptions (pre-fix CLI startup vs post-fix persistent
session).

Usage:
  python3 scripts/profile_pipeline.py [--model-delay 0.0] [--fetch-delay 0.5]
      [--leaves 5] [--json out.json]

The output table is the measurement the standing role acts on: which stage,
how many seconds, how many model calls, how many bytes fetched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agp import Domain  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import PipelineModel  # noqa: E402

QUESTION = ("Will Apple report quarterly results above Wall Street consensus "
            "expectations in its next earnings report?")

OPENALEX_BODY = json.dumps({
    "results": [
        {"id": f"W{i}",
         "title": "Scholarly study on apple earnings expectations: a "
                  "literature review of analyst consensus and quarterly "
                  f"results {i}",
         "publication_year": 2024, "cited_by_count": 12}
        for i in range(3)
    ],
})
FR_BODY = json.dumps({
    "documents": [
        {"title": "Final agency rule on apple earnings disclosure and "
                  "quarterly results expectations: proposed and final rules",
         "document_number": "2024-12345", "published_at": "2024-01-15",
         "agency": "government agency"},
    ],
})


def _routes() -> dict[str, str]:
    return {"/works": OPENALEX_BODY, "/documents.json": FR_BODY}


def _decompose_response(n_leaves: int) -> str:
    subs = []
    kinds = ["descriptive", "causal"]
    qtypes = ["scholarly work search",
              "final/proposed agency rules with dates and docket refs"]
    for i in range(n_leaves):
        subs.append({
            "text": f"sub-question {i}: what does the evidence say about "
                    "apple earnings expectations and analyst consensus",
            "kind": kinds[i % 2],
            "question_type": qtypes[i % 2],
            "min_source_tier": 1 if i % 2 else 2,
            "min_independent_sources": 1,
            "quant_required": False,
            "horizon_days": None,
        })
    return json.dumps({"sub_questions": subs})


def _answer(conf: float) -> str:
    return json.dumps({"answer": "the evidence supports the claim",
                       "proposed_confidence": conf, "compute": None})


class InstrumentedModel(PipelineModel):
    """Scripted responses + schedule recording + simulated latency."""

    name = "instrumented"

    def __init__(self, decompose: str, n_answers: int,
                 model_delay_s: float):
        self._responses = {"Architect": [decompose]}
        self._answers = [_answer(0.7) for _ in range(n_answers + 4)]
        self.model_delay_s = model_delay_s
        self.schedule: list[dict] = []

    async def complete(self, role: str, messages: list[dict],
                       **_ignored) -> dict:
        prompt = "\n".join(m.get("content", "") for m in messages)
        entry = {"kind": "model", "role": role,
                 "prompt_chars": len(prompt), "t0": time.monotonic()}
        if self.model_delay_s:
            await asyncio.sleep(self.model_delay_s)
        if role == "Architect":
            content = self._responses["Architect"][0]
        else:
            content = self._answers.pop(0)
        entry["t1"] = time.monotonic()
        entry["resp_chars"] = len(content)
        entry["dur"] = entry["t1"] - entry["t0"]
        self.schedule.append(entry)
        return {"content": content}


class InstrumentedTransport:
    """fixture_transport + byte/duration accounting + simulated latency."""

    def __init__(self, routes: dict[str, str], fetch_delay_s: float):
        self._inner = fixture_transport(routes)
        self.fetch_delay_s = fetch_delay_s
        self.schedule: list[dict] = []

    def __call__(self, url: str, headers: dict) -> tuple[int, str]:
        t0 = time.monotonic()
        if self.fetch_delay_s:
            time.sleep(self.fetch_delay_s)
        status, body = self._inner(url, headers)
        self.schedule.append({
            "kind": "fetch", "url": url[:120], "status": status,
            "bytes": len(body.encode("utf-8")),
            "dur": time.monotonic() - t0,
            "t0": t0, "t1": time.monotonic(),
        })
        return status, body


def _quiet_adversary():
    class _Q:
        async def complete(self, task_class, messages, schema=None):
            return {"parsed_json": {"objections": []}, "model": "stub"}
    return _Q()


async def _run(model_delay: float, fetch_delay: float, n_leaves: int,
               parallel: bool) -> dict:
    from tools.pipeline import engine as engine_mod

    orig = engine_mod.ResearchPipeline._fetch_for_question
    # The `parallel` switch is injected by monkeypatching ONLY when testing
    # the optimized variant; default path is untouched production code.
    if parallel:
        raise SystemExit("parallel variant is the fixed engine, not a flag")

    ledger = ProvenanceLedger()
    store = ArtifactStore(root=Path("/tmp") / f"speed_profile_art")
    model = InstrumentedModel(_decompose_response(n_leaves), n_leaves * 2,
                              model_delay)
    transport = InstrumentedTransport(_routes(), fetch_delay)
    pipeline = ResearchPipeline(
        model=model, adversary_router=_quiet_adversary(),
        transport=transport, store=store, ledger=ledger)

    wall_t0 = time.monotonic()
    result = await pipeline.run(QUESTION, domain=Domain.FINANCIAL,
                                today=date(2026, 8, 22))
    wall = time.monotonic() - wall_t0
    return {
        "wall_s": round(wall, 3),
        "sealed": result.sealed,
        "refusal_reason": result.refusal_reason,
        "n_leaves": len(result.leaves),
        "n_fetches": len(result.fetches),
        "confidence": result.confidence_score,
        "conclusion": result.conclusion,
        "notes": list(result.notes),
        "model_schedule": model.schedule,
        "fetch_schedule": transport.schedule,
        "session_dict_len": len(json.dumps(result.session.to_dict()
                                           if result.session else {})),
        "_result": result,
    }


def _stage_table(profile: dict) -> list[tuple[str, float, int, int]]:
    """(stage, seconds_on_critical_path, model_calls, bytes_fetched).

    Attribution is structural: every model call and fetch is assigned to the
    stage whose work contains it (decompose / leaf k / adversary), using the
    recorded start offsets against the leaf boundaries implied by order.
    """
    sched = sorted(profile["model_schedule"] + profile["fetch_schedule"],
                   key=lambda e: e["t0"])
    rows: list[tuple[str, float, int, int]] = []
    arch = [e for e in sched if e.get("role") == "Architect"]
    managers = [e for e in sched if e.get("role") == "Manager"]
    advs = [e for e in sched if e.get("role") not in
            ("Architect", "Manager", None)]
    fetches = [e for e in sched if e["kind"] == "fetch"]
    t_end_arch = max((e["t1"] for e in arch), default=0.0)
    t0_adv = min((e["t0"] for e in advs), default=float("inf"))

    def span(entries, lo=None, hi=None):
        sel = [e for e in entries
               if (lo is None or e["t0"] >= lo) and (hi is None or e["t0"] < hi)]
        dur = sum(e["dur"] for e in sel)
        return dur, len(sel)

    d_dur, d_n = span(arch)
    rows.append(("decompose", d_dur, d_n, 0))
    # leaf sections are delimited by Manager calls: everything between the
    # last Architect byte and the first non-Manager model call is leaf work.
    leaf_fetches = [e for e in fetches if e["t0"] >= t_end_arch
                    and e["t0"] < (t0_adv if t0_adv != float("inf") else 9e18)]
    m_dur, m_n = span(managers)
    bytes_fetched = sum(e["bytes"] for e in leaf_fetches)
    f_dur, f_n = span(leaf_fetches)
    rows.append((f"leaves({profile['n_leaves']}) fetch+answer", m_dur + f_dur,
                 m_n, bytes_fetched))
    a_dur, a_n = span(others)
    rows.append(("adversary(+seal)", a_dur, a_n, 0))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-delay", type=float, default=0.0)
    ap.add_argument("--fetch-delay", type=float, default=0.0)
    ap.add_argument("--leaves", type=int, default=5)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    prof = asyncio.run(_run(args.model_delay, args.fetch_delay, args.leaves,
                            parallel=False))
    print(f"question: {QUESTION[:70]}...")
    print(f"sealed={prof['sealed']} leaves={prof['n_leaves']} "
          f"fetches={prof['n_fetches']} conf={prof['confidence']} "
          f"wall={prof['wall_s']}s")
    print()
    hdr = f"{'stage':<28}{'busy_s':>9}{'calls':>7}{'bytes':>10}"
    print(hdr)
    print("-" * len(hdr))
    for stage, secs, calls, nbytes in _stage_table(prof):
        print(f"{stage:<28}{secs:>9.3f}{calls:>7}{nbytes:>10}")
    print(f"{'TOTAL busy':<28}"
          f"{sum(r[1] for r in _stage_table(prof)):>9.3f}"
          f"{sum(r[2] for r in _stage_table(prof)):>7}"
          f"{sum(r[3] for r in _stage_table(prof)):>10}")
    print(f"\nwall clock (serial critical path): {prof['wall_s']}s")

    n_model = len(prof["model_schedule"])
    n_fetch = len(prof["fetch_schedule"])
    total_bytes = sum(e["bytes"] for e in prof["fetch_schedule"])
    print(f"model calls: {n_model}   fetches: {n_fetch}   "
          f"bytes fetched: {total_bytes}")

    if args.json:
        out = {k: v for k, v in prof.items() if k != "_result"}
        Path(args.json).write_text(json.dumps(out, indent=2, default=str))
        print(f"wrote {args.json}")

    # fingerprint for the answer-did-not-change check
    r = prof["_result"]
    fp = {
        "sealed": r.sealed,
        "refusal_reason": r.refusal_reason,
        "confidence_score": r.confidence_score,
        "conclusion": r.conclusion,
        "leaves": [{"qid": l.question_id, "answer": l.answer,
                    "conf": l.confidence, "tier": l.tier,
                    "sources": l.source_classes}
                   for l in r.leaves],
        "fetch_urls": [f.url for f in r.fetches],
        "evidence": ([e.content for e in r.session.evidence]
                     if r.session else []),
        "objections": [getattr(o, "text", str(o)) for o in r.objections],
    }
    Path("/tmp/speed_profile_fingerprint.json").write_text(
        json.dumps(fp, indent=2, sort_keys=True))
    print("fingerprint: /tmp/speed_profile_fingerprint.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
