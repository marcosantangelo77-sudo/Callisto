"""I4 — the retrodiction BATCH runner.

This module turns the retrodiction harness from a test rig into an asset
factory: it runs N questions through the real pipeline
(tools/pipeline/retro.PipelineResearcher over tools/pipeline/engine), scores
each against its known outcome, and writes resolved, scored records to BOTH
stores that downstream capabilities read:

  - tools/routing/scores.ModelScoreStore   (empirical model routing)
  - a batch results JSONL                  (reporting / error map /
                                            inheritance-rule descendants)

Design constraints:

  1. RESUMABLE. One question = one unit of work. Every completed question is
     checkpointed (tools/pipeline/checkpoint.FileCheckpointer, reused — no new
     checkpoint machinery) with stage "retro_batch". A killed run re-reads the
     checkpoint store on startup and skips everything already done. Never
     redo completed work; never lose more than the in-flight question.

  2. CHEAP TO INTERRUPT. Progress is written after EVERY question, so Ctrl-C
     costs at most one question (~4 min).

  3. MAGNITUDE SCORING. Per NEXT.md RETRODICTION SCORING: where a market
     exists at claim time, score against magnitude, not direction. The
     question carries `market_implied_probability` (the devigged fair prob
     implied by prices at claim time); the score is the continuous Brier
     (probability-odds scoring) against the realised binary plus an edge term
     |p_model − p_market| credited when the model's direction was right and
     debited when wrong. Binary-only questions keep plain Brier.

  4. HONEST NULLS. A question whose evidence cannot prove pre-cutoff
     publication yields a NULL result — recorded as such in the results file,
     counted in the report, never silently dropped.

  5. READABLE REPORT. Accuracy + calibration overall, sliced by domain,
     horizon band, and question type — the slice table IS the error map.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from tools.pipeline.checkpoint import FileCheckpointer, hash_inputs
from tools.retrodiction.questions import RetrodictionQuestion
from tools.retrodiction.scoring import (
    Prediction,
    calibration_curve,
    resolved_claim_record,
    score_brier,
    slice_breakdown,
)

logger = logging.getLogger("callisto.retrodiction.batch")


# ── Magnitude scoring ──────────────────────────────────────────────────────

def magnitude_score(probability: float, answer_binary: bool,
                    market_implied: Optional[float]) -> Optional[dict]:
    """Continuous score of a prediction against a market benchmark.

    Returns None when no market exists (caller falls back to binary Brier).
    Components:
      realized_brier      — (p − y)^2, continuous in p (same as binary Brier;
                            kept separate so market slices compare cleanly)
      edge_taken          — p_model − p_market (signed; this is what a bet
                            would have been sized on)
      directional_edge    — +|p_model − p_market| if the model's direction was
                            RIGHT, −|…| if WRONG. Mean > 0 across a corpus
                            means the predictions beat the market's implied
                            distribution — CLV, generalised, exactly per
                            NEXT.md. This is the information-dense number:
                            each observation says HOW MUCH right, not merely
                            whether.
    """
    if market_implied is None:
        return None
    if not (0.0 <= market_implied <= 1.0):
        raise ValueError("market_implied must be in [0,1]")
    y = 1.0 if answer_binary else 0.0
    edge = probability - market_implied
    right = (edge > 0) == bool(y)
    return {
        "realized_brier": round((probability - y) ** 2, 6),
        "market_implied": round(market_implied, 6),
        "edge_taken": round(edge, 6),
        "directional_edge": round(abs(edge) if right else -abs(edge), 6),
    }


# ── Researcher call seam ───────────────────────────────────────────────────

async def _call_researcher(researcher, prompts: list[dict]) -> list:
    """Invoke a researcher's answer() whether it is sync or async.

    The batch runner is async; the real PipelineResearcher.answer() is a
    SYNC method that internally runs its own event loop. Calling that from a
    running loop raises 'RuntimeError: This event loop is already running'
    — which is exactly how every question in the first live batch failed.
    Sync researchers are executed on a worker thread so they may freely own
    their own loop; async-native researchers are awaited directly.
    """
    answer = getattr(researcher, "answer")
    if inspect.iscoroutinefunction(answer):
        return await answer(prompts, [], loops=1)
    return await asyncio.to_thread(answer, prompts, [], 1)


# ── Result record ──────────────────────────────────────────────────────────

@dataclass
class BatchResult:
    """One question's outcome. status ∈ {scored, null, refused, error}."""
    question_id: str
    status: str
    text: str = ""
    domain: str = "GENERAL"
    question_type: str = ""
    horizon_band: str = ""
    predicted_probability: Optional[float] = None
    answer_binary: Optional[bool] = None
    brier: Optional[float] = None
    magnitude: Optional[dict] = None
    sealed: bool = False
    refusal_reason: str = ""
    n_fetches: int = 0
    objections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str = ""
    elapsed_s: float = 0.0
    recorded_at: str = ""

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


def horizon_band(days: int) -> str:
    return ("short" if days <= 45 else
            "medium" if days <= 120 else "long")


# ── The runner ─────────────────────────────────────────────────────────────

@dataclass
class BatchConfig:
    label: str = "default"
    model_name: str = "hermes-cli"
    role: str = "pipeline"           # routing-store role under which scores land
    signing_key: str = ""            # passed to proof minting, not required
    limit: int = 0                   # 0 = all questions
    on_progress: Optional[Callable[[int, int], None]] = None


class RetrodictionBatch:
    """Run questions through the pipeline, checkpoint per question, resume."""

    STAGE = "retro_batch"

    def __init__(self, *, questions: list[RetrodictionQuestion],
                 researcher_factory: Callable[[], object],
                 checkpointer: FileCheckpointer,
                 results_path: Path | str,
                 config: Optional[BatchConfig] = None):
        self.questions = list(questions)
        self.researcher_factory = researcher_factory
        self.checkpointer = checkpointer
        self.results_path = Path(results_path)
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or BatchConfig()
        # question_id -> BatchResult for this process's lifetime
        self.results: dict[str, BatchResult] = {}

    # -- identity / resumability --

    def _run_key(self, q: RetrodictionQuestion) -> str:
        from tools.pipeline.checkpoint import run_key
        return run_key(q.prompt_for_researcher()["text"], q.domain,
                       q.claim_date.isoformat() if q.claim_date else "")

    def _inputs_hash(self, q: RetrodictionQuestion) -> str:
        return hash_inputs({"question_id": q.question_id,
                            "claim_date": (q.claim_date.isoformat()
                                           if q.claim_date else ""),
                            "config": self.config.label})

    def load_completed(self) -> dict[str, dict]:
        """Rehydrate prior results from the checkpoint store AND the results
        JSONL (either surviving alone is enough — belt and braces).

        Only terminal statuses count as complete: 'error' rows are retried
        on resume, everything else (scored/null/refused) is final."""
        done: dict[str, dict] = {}
        for ck in self.checkpointer.list_all():
            if ck.stage == self.STAGE and isinstance(ck.payload, dict) \
                    and ck.payload.get("status") \
                    and ck.payload.get("status") != "error":
                qid = ck.payload.get("question_id")
                if qid:
                    done[qid] = ck.payload
        if self.results_path.exists():
            for line in self.results_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn final line: skipped, not fatal
                qid = rec.get("question_id")
                if qid and rec.get("status") and rec.get("status") != "error":
                    done[qid] = rec
        return done

    # -- append-only results --

    def _append(self, result: BatchResult) -> None:
        result.recorded_at = datetime.now(timezone.utc).isoformat()
        with open(self.results_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")

    # -- one question --

    async def _run_one(self, q: RetrodictionQuestion) -> BatchResult:
        t0 = time.monotonic()
        base = dict(question_id=q.question_id, text=q.text,
                    domain=q.domain, question_type=q.question_type.value,
                    horizon_band=horizon_band(q.horizon_days))
        try:
            researcher = self.researcher_factory()
            prompts = [q.prompt_for_researcher()]
            preds = await _call_researcher(researcher, prompts)
            pred = next((p for p in preds
                         if p.question_id == q.question_id), None)
        except Exception as e:  # noqa: BLE001 — a failed question is a row,
            return BatchResult(  # not a dead batch
                status="error", error=f"{type(e).__name__}: {e}",
                elapsed_s=time.monotonic() - t0, **base)

        elapsed = time.monotonic() - t0
        if pred is None:
            return BatchResult(status="null",
                               refusal_reason="researcher returned no "
                                              "prediction",
                               elapsed_s=elapsed, **base)

        result = BatchResult(
            status="scored", predicted_probability=pred.probability,
            answer_binary=bool(q.answer_binary),
            brier=round((pred.probability -
                         (1.0 if q.answer_binary else 0.0)) ** 2, 6),
            magnitude=magnitude_score(pred.probability, q.answer_binary,
                                      q.market_implied),
            elapsed_s=elapsed, **base)
        # enrich from the researcher's own run trace where available
        pr = getattr(researcher, "results", None)
        if pr:
            r = pr[-1]
            result.sealed = bool(getattr(r, "sealed", False))
            result.refusal_reason = str(getattr(r, "refusal_reason", "") or "")
            result.n_fetches = len(getattr(r, "fetches", []) or [])
            result.objections = [getattr(o, "text", str(o))
                                 for o in (getattr(r, "objections", []) or [])]
            result.notes = list(getattr(r, "notes", []) or [])
            if not result.sealed and not result.refusal_reason:
                result.refusal_reason = "pipeline did not seal"
        return result

    # -- the batch --

    async def run(self) -> dict[str, BatchResult]:
        done = self.load_completed()
        # qids already present in the results FILE (checkpoint-only completions
        # get re-appended below so the JSONL stays the full record).
        from_file: set[str] = set()
        if self.results_path.exists():
            for line in self.results_path.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        from_file.add(json.loads(line).get("question_id"))
                    except json.JSONDecodeError:
                        continue
        qs = self.questions
        if self.config.limit:
            qs = qs[:self.config.limit]
        todo = [q for q in qs if q.question_id not in done]
        logger.info("batch %s: %d total, %d already complete, %d to run",
                    self.config.label, len(qs), len(qs) - len(todo), len(todo))

        appended: set[str] = set()

        def _append_once(res: BatchResult) -> None:
            if res.question_id in from_file or res.question_id in appended:
                return
            appended.add(res.question_id)
            self._append(res)

        for i, q in enumerate(todo):
            rk = self._run_key(q)
            ih = self._inputs_hash(q)
            hit = self.checkpointer.load(rk, self.STAGE, ih)
            if (hit is not None and hit.payload.get("status")
                    and hit.payload.get("status") != "error"):
                payload = hit.payload
                _append_once(BatchResult(
                    **{k: v for k, v in payload.items()
                       if k in BatchResult.__dataclass_fields__}))
            else:
                result = await self._run_one(q)
                payload = result.to_dict()
                self.checkpointer.save(rk, self.STAGE, ih, payload,
                                       claim_ids=[q.question_id])
                _append_once(result)
                self.results[q.question_id] = result
            if self.config.on_progress:
                self.config.on_progress(i + 1, len(todo))
        # merge resumed rows into self.results for reporting AND ensure the
        # results JSONL carries every completion (a run where everything was
        # already checkpointed still leaves a complete file behind).
        completed = self.load_completed()
        for qid, rec in completed.items():
            if qid not in self.results:
                self.results[qid] = BatchResult(
                    **{k: v for k, v in rec.items()
                       if k in BatchResult.__dataclass_fields__})
            _append_once(self.results[qid])
        # error rows from earlier runs stay in the results file (history is
        # never rewritten) but are NOT counted as completed work — they were
        # re-run above and the fresh outcome supersedes them in self.results.
        return self.results


# ── Reporting ──────────────────────────────────────────────────────────────

def build_report(results: dict[str, BatchResult]) -> dict:
    """Overall accuracy + calibration, sliced by domain, horizon, type.

    Nulls are counted, never hidden: n_attempted vs n_scored is part of the
    headline. A slice table dominated by nulls is reported AS a retrieval
    finding."""
    rows = list(results.values())
    scored = [r for r in rows if r.status == "scored"]
    nulls = [r for r in rows if r.status != "scored"]

    def _calibration(items: list[BatchResult], n_bins: int = 5) -> list[dict]:
        out = []
        width = 1.0 / n_bins
        for i in range(n_bins):
            lo, hi = i * width, (i + 1) * width
            bucket = [(r.predicted_probability,
                       _implied_outcome(r)) for r in items
                      if lo <= r.predicted_probability < hi
                      or (i == n_bins - 1 and r.predicted_probability == 1.0)]
            entry = {"bin_low": round(lo, 2), "bin_high": round(hi, 2),
                     "n": len(bucket), "mean_p": None, "realised": None}
            if bucket:
                entry["mean_p"] = round(sum(p for p, _ in bucket)
                                        / len(bucket), 4)
                entry["realised"] = round(sum(y for _, y in bucket)
                                          / len(bucket), 4)
            out.append(entry)
        return out

    from tools.retrodiction.scoring import (
        brier_decomposition,
        bootstrap_brier_ci,
    )
    scored_preds = [
        Prediction(question_id=r.question_id,
                   probability=r.predicted_probability or 0.5)
        for r in scored]
    qs_by_id = {}
    for res in results.values():
        if res.status == "scored" and res.answer_binary is not None:
            qs_by_id[res.question_id] = RetrodictionQuestion(
                question_id=res.question_id, answer_binary=res.answer_binary)
    questions = list(qs_by_id.values())
    try:
        decomp = brier_decomposition(scored_preds, questions)
        decomp = {k: (round(v, 6) if isinstance(v, float) else v)
                  for k, v in decomp.items()}
    except ValueError:
        decomp = None
    try:
        ci_lo, ci_hi = bootstrap_brier_ci(scored_preds, questions)
        brier_ci = [round(ci_lo, 4), round(ci_hi, 4)]
    except ValueError:
        brier_ci = None

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_total": len(rows),
        "n_scored": len(scored),
        "n_null": len(nulls),
        "null_rate": round(len(nulls) / len(rows), 4) if rows else None,
        "mean_brier": (round(sum(r.brier for r in scored) / len(scored), 6)
                       if scored else None),
        "brier_ci95": brier_ci,
        "mean_absolute_error_vs_half": (
            round(sum(abs((r.predicted_probability or 0.5) - 0.5)
                      for r in scored) / len(scored), 6) if scored else None),
        "sealed_rate": (round(sum(1 for r in scored if r.sealed)
                              / len(scored), 4) if scored else None),
        "mean_elapsed_s": (round(sum(r.elapsed_s for r in scored)
                                 / len(scored), 1) if scored else None),
        "magnitude": _magnitude_summary(scored),
        "brier_decomposition": decomp,
        "calibration_overall": _calibration(scored),
        "slices": {
            "by_domain": _slice_table(scored, "domain"),
            "by_horizon": _slice_table(scored, "horizon_band"),
            "by_question_type": _slice_table(scored, "question_type"),
            "nulls_by_domain": _count_by(nulls, "domain"),
            "errors_by_domain": _count_by(
                [r for r in nulls if r.status == "error"], "domain"),
        },
        "failures": {
            "refusals": [{"question_id": r.question_id,
                          "reason": r.refusal_reason[:200]}
                         for r in rows if r.status == "refused"],
            "errors": [{"question_id": r.question_id,
                        "error": r.error[:200]} for r in rows
                       if r.status == "error"],
        },
    }
    report["verdict"] = _verdict(report)
    return report


def _implied_outcome(r: BatchResult) -> float:
    """The realised binary for this row. Stored answer_binary when present;
    otherwise invert brier = (p − y)^2 → y ∈ {0,1}."""
    p = r.predicted_probability or 0.5
    if r.answer_binary is not None:
        return 1.0 if r.answer_binary else 0.0
    if r.brier is None:
        return 1.0 if p >= 0.5 else 0.0
    root = max(0.0, min(1.0, r.brier)) ** 0.5
    for y in (p - root, p + root):
        cand = round(y)
        if abs(p - cand) <= root + 1e-9 and cand in (0.0, 1.0):
            return cand
    return 1.0 if p >= 0.5 else 0.0


def _slice_table(rows: list["BatchResult"], key: str) -> dict:
    out: dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(str(getattr(r, key)), {"n": 0, "briers": [],
                                                  "edges": []})
        s["n"] += 1
        if r.brier is not None:
            s["briers"].append(r.brier)
        if r.magnitude:
            s["edges"].append(r.magnitude["directional_edge"])
    table = {}
    for k, v in out.items():
        b = v.pop("briers")
        e = v.pop("edges")
        table[k] = {
            "n": v["n"],
            "brier": round(sum(b) / len(b), 4) if b else None,
            "mean_directional_edge": (round(sum(e) / len(e), 4) if e
                                      else None),
        }
    return table


def _count_by(rows: list["BatchResult"], key: str) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(getattr(r, key))] = counts.get(str(getattr(r, key)), 0) + 1
    return counts


def _magnitude_summary(scored: list["BatchResult"]) -> dict:
    mags = [(r.magnitude, _implied_outcome(r)) for r in scored
            if r.magnitude]
    if not mags:
        return {"n_with_market": 0}
    edges = [m["directional_edge"] for m, _ in mags]
    taken = [m["edge_taken"] for m, _ in mags]
    return {
        "n_with_market": len(mags),
        "mean_edge_taken": round(sum(taken) / len(taken), 6),
        "mean_directional_edge": round(sum(edges) / len(edges), 6),
        "beat_market_rate": round(sum(1 for e in edges if e > 0)
                                  / len(edges), 4),
    }


def _verdict(report: dict) -> str:
    if not report["n_scored"]:
        return ("NO SCORED OBSERVATIONS — every question produced a null. "
                "This is a retrieval/provenance finding, not silence.")
    if report["null_rate"] is not None and report["null_rate"] > 0.5:
        return ("MAJORITY NULLS — most evidence could not prove pre-cutoff "
                "publication; treat scores as unrepresentative and fix "
                "retrieval first.")
    mb = report["mean_brier"]
    if mb is None:
        return "incomplete"
    if mb < 0.20:
        return "strongly better than chance (Brier < 0.20)"
    if mb < 0.25:
        return "better than chance (Brier < 0.25)"
    return "at or worse than chance — do not trust these predictions yet"


# ── Routing-store bridge ───────────────────────────────────────────────────

def write_routing_scores(results: dict[str, BatchResult],
                         score_store, *,
                         role: str = "pipeline",
                         model: str = "hermes-cli",
                         task_class: str = "research_synthesis") -> int:
    """Append every scored observation into ModelScoreStore so empirical
    routing has measurements. Nulls/errors are NOT written — absence of a
    record is honest; a fabricated loss would be flattery either way."""
    n = 0
    for r in results.values():
        if r.status != "scored" or r.brier is None:
            continue
        score_store.record(role=role, model=model, task_class=task_class,
                           question_id=r.question_id, brier=r.brier,
                           predicted_probability=r.predicted_probability,
                           source="retrodiction_batch")
        n += 1
    return n


# ── Human-readable rendering ───────────────────────────────────────────────

def render_report(report: dict) -> str:
    L = []
    L.append("=" * 68)
    L.append("RETRODICTION BATCH REPORT")
    L.append(f"generated {report['generated_at']}")
    L.append("=" * 68)
    L.append(f"questions: {report['n_total']}  scored: {report['n_scored']}  "
             f"nulls/errors: {report['n_null']}  "
             f"(null rate {report['null_rate']})")
    if report["mean_brier"] is not None:
        L.append(f"mean Brier: {report['mean_brier']}"
                 + (f"  (95% CI {report['brier_ci95'][0]}–"
                    f"{report['brier_ci95'][1]})"
                    if report.get("brier_ci95") else "")
                 + f"   sealed rate: {report['sealed_rate']}   "
                 f"mean {report['mean_elapsed_s']}s/question")
    dec = report.get("brier_decomposition")
    if dec:
        L.append(f"Brier decomposition: reliability {dec['reliability']} "
                 f"(calibration error) · resolution {dec['resolution']} "
                 f"(signal) · uncertainty {dec['uncertainty']} (floor)")
        if dec["reliability"] < 0.02 and dec["resolution"] < 0.01:
            L.append("  → honest but uninformative: predictions carry almost "
                     "no signal beyond the base rate")
        elif dec["reliability"] > 2 * (dec["resolution"] or 1e-9) \
                and dec["resolution"] > 0:
            L.append("  → miscalibration dominates the score; fix confidence, "
                     "not retrieval")
    mag = report["magnitude"]
    if mag.get("n_with_market"):
        L.append(f"magnitude vs market (n={mag['n_with_market']}): "
                 f"mean directional edge {mag['mean_directional_edge']}  "
                 f"beat-market rate {mag['beat_market_rate']}")
    L.append("")
    L.append("VERDICT: " + report["verdict"])
    for name, table in (("BY DOMAIN", report["slices"]["by_domain"]),
                        ("BY HORIZON", report["slices"]["by_horizon"]),
                        ("BY QUESTION TYPE",
                         report["slices"]["by_question_type"])):
        if not table:
            continue
        L.append("")
        L.append(f"-- {name} --")
        L.append(f"{'slice':<24}{'n':>5}{'brier':>10}{'mkt_edge':>10}")
        for k, v in sorted(table.items()):
            L.append(f"{k:<24}{v['n']:>5}"
                     f"{v['brier'] if v['brier'] is not None else '-':>10}"
                     f"{v['mean_directional_edge'] if v['mean_directional_edge'] is not None else '-':>10}")
    nb = report["slices"]["nulls_by_domain"]
    if nb:
        L.append("")
        L.append("-- NULLS BY DOMAIN (a retrieval finding, shown not buried) --")
        for k, v in sorted(nb.items()):
            L.append(f"  {k:<24}{v}")
    fails = report["failures"]
    if fails["errors"]:
        L.append("")
        L.append("-- ERRORS --")
        for f_ in fails["errors"]:
            L.append(f"  {f_['question_id']}: {f_['error']}")
    if fails["refusals"]:
        L.append("")
        L.append("-- REFUSALS --")
        for f_ in fails["refusals"]:
            L.append(f"  {f_['question_id']}: {f_['reason']}")
    cal = report["calibration_overall"]
    live_bins = [b for b in cal if b["n"]]
    if live_bins:
        L.append("")
        L.append("-- CALIBRATION --")
        L.append(f"{'bin':<16}{'n':>5}{'mean_p':>9}{'realised':>9}")
        for b in live_bins:
            bin_label = f"[{b['bin_low']:.1f},{b['bin_high']:.1f})"
            L.append(f"{bin_label:<16}"
                     f"{b['n']:>5}{b['mean_p']:>9}"
                     f"{b['realised']:>9}")
    return "\n".join(L)
