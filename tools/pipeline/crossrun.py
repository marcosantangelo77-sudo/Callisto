"""Cross-run memory — what a run LEARNED about its SOURCES, carried forward.

Every run currently starts cold: it re-fetches from sources that returned
nothing for this kind of question last time, re-errors against the same
broken endpoint, re-spends the same wasted calls. This module persists a
small STRUCTURED record at the end of each run — per-source outcome counts,
per-leaf gap kinds, final stance and tier — and loads the records for the
same QUESTION CLASS at the start of the next one, as an ORDER HINT over
candidate sources.

GATE RULES (this is where a memory system goes wrong; each is structural,
not conventional):

1. ORDER ONLY. Memory may inform WHAT to research and in WHAT ORDER —
   nothing else. The only live-run consumer is ``PlanningView.order_specs``,
   a stable partition that moves chronically-null sources to the BACK of
   the fan-out. It cannot exclude, cannot inject, cannot re-rank by
   anything but "deprioritised or not". A fragile (retrieval-failure-prone)
   source is FLAGGED in the run notes; it is still fetched.

2. NEVER CONFIDENCE. No remembered value may raise a confidence score, seed
   a prior, or substitute for current-run evidence. ``PlanningView`` is the
   ONLY object handed to a live run and it physically carries no stance,
   tier, conclusion, or content — there is nothing in it that COULD move a
   score. (The store's records do carry stance/tier, as run-outcome facts
   for audit; the load path strips them before any consumer sees them.)

3. NOT EVIDENCE. A remembered fact never enters the ledger, is never
   cited, and never counts toward independence. Nothing here imports or
   constructs Evidence; nothing here writes to a ProvenanceLedger.

4. PER QUESTION CLASS, NOT PER QUESTION. Records are keyed by the run's
   task class (tools.task_classifier) — the same slicing vocabulary the
   routing store uses for (role, task_class). The root query is stored only
   as a truncated SHA for audit and is NEVER part of the lookup key, so a
   different question of the same class shares the memory and no answer
   text can leak across a retrodiction cutoff.

Persistence follows the ModelScoreStore convention (tools/routing/scores.py):
append-only JSONL, one record per line, torn last line skipped, thread-
locked, state off the OneDrive tree via CALLISTO_STATE_DIR. No new storage
engine is introduced.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1

#: A source is deprioritised only after this many class-runs in which it
#: appeared and admitted NOTHING. Below this the sample is too thin to
#: reorder fan-out on — the spec'd example is "three times running".
DEPRIORITISE_MIN_RUNS = 3

#: How many most-recent records per class the view considers. Old failures
#: age out; a source can redeem itself.
DEPRIORITISE_WINDOW = 10

#: Fragile flag: a source whose fetch attempts mostly ERROR (retrieval
#: failure, not honest null) is reported, never reordered away.
FRAGILE_MIN_ERRORS = 2
FRAGILE_MIN_RATE = 0.5


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def question_class_for(query: str) -> str:
    """The run's question CLASS — the coarse bucket memory is keyed on.

    tools.task_classifier is the existing canonical bucketing (the same
    vocabulary the orchestrator and routing store already use); reusing it
    avoids inventing a second notion of class.
    """
    from tools.task_classifier import classify_query
    return classify_query(query).value


# ── The store ──────────────────────────────────────────────────────────────

def default_path() -> Path:
    base = os.getenv("CALLISTO_STATE_DIR") or os.path.expanduser(
        "~/.local/state/callisto")
    return Path(base) / "crossrun" / "runs.jsonl"


class CrossRunMemoryStore:
    """Append-only JSONL of per-run source-outcome records."""

    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path is not None else default_path()
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> dict:
        line = json.dumps(record, sort_keys=True)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return record

    def load_class(self, question_class: str) -> list[dict]:
        """Intact records for EXACTLY this class, oldest first.

        The class string is the whole key: no question text, no hash of the
        question, nothing finer-grained — per-question memory is a cache
        and would leak answers across a retrodiction cutoff.
        """
        if not self.path.exists():
            return []
        out: list[dict] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue          # torn final line: skip, not fatal
                if isinstance(rec, dict) and \
                        rec.get("question_class") == question_class and \
                        rec.get("v") == SCHEMA_VERSION:
                    out.append(rec)
        return out


# ── The record (JOB 1: persist at end of run) ─────────────────────────────

def record_run(result: Any, leaf_traces: dict, question_class: str,
               root_query: str) -> dict:
    """One structured record of what this run's SOURCES did. Facts, not
    prose; no evidence content, no conclusions.

    `result` is a PipelineResult; `leaf_traces` maps question_id ->
    RetrievalTrace (the per-leaf fetch history the engine collected).
    """
    sources: dict[str, dict[str, int]] = {}

    def bucket(name: str) -> dict:
        return sources.setdefault(name, {"admitted": 0, "rejected_gate": 0,
                                         "errored": 0, "skipped": 0})

    for f in getattr(result, "fetches", None) or []:
        bucket(f.source_name)["admitted"] += 1
    for tr in (leaf_traces or {}).values():
        for rd in getattr(tr, "rounds", None) or []:
            for s in rd.get("sources", []) or []:
                name = s.get("name") or ""
                if not name:
                    continue
                b = bucket(name)
                if "error" in s:
                    b["errored"] += 1
                elif s.get("skipped"):
                    b["skipped"] += 1
                elif "rejected" in s:
                    b["rejected_gate"] += 1

    return {
        "v": SCHEMA_VERSION,
        "recorded_at": _utcnow_iso(),
        "question_class": question_class,
        "sources": sources,
        # gap_kind per leaf: honest_null / retrieval_failure / unprovable,
        # "" for a leaf answered on real evidence.
        "gap_kinds": {l.question_id: (getattr(l, "gap_kind", "") or "")
                      for l in getattr(result, "leaves", None) or []},
        "stance": getattr(result, "stance", "UNDETERMINED"),
        "tier": getattr(result, "confidence_tier", "UNVERIFIED"),
        "sealed": bool(getattr(result, "sealed", False)),
        "refusal_reason": (getattr(result, "refusal_reason", "") or "")[:200],
        "n_fetches": len(getattr(result, "fetches", None) or []),
        # Audit only. NEVER part of the lookup key.
        "root_query_sha256": hashlib.sha256(
            (root_query or "").encode("utf-8")).hexdigest()[:16],
    }


# ── The view (JOB 2: load at start of run) ────────────────────────────────

class PlanningView:
    """The ONLY shape cross-run memory hands to a live run.

    Carries source names and behavioral counts, and can do exactly one
    thing to them: a STABLE PARTITION that moves chronic-null sources to
    the back of the candidate list. No stance, tier, conclusion, or
    content exists on this object, so no consumer can accidentally (or
    deliberately) turn yesterday's run into today's confidence — the
    trust-escalator red-team R5 closed stays closed.
    """

    def __init__(self, question_class: str, records: Iterable[dict]):
        self.question_class = question_class
        recent = list(records)[-DEPRIORITISE_WINDOW:]
        self.runs_considered = len(recent)

        agg: dict[str, dict] = {}
        for rec in recent:
            for name, c in (rec.get("sources") or {}).items():
                a = agg.setdefault(name, {"runs": 0, "admitted": 0,
                                          "errored": 0})
                if sum(int(v) for v in c.values()) > 0:
                    a["runs"] += 1
                a["admitted"] += int(c.get("admitted") or 0)
                a["errored"] += int(c.get("errored") or 0)

        self._null_runs: dict[str, int] = {
            name: a["runs"] for name, a in agg.items()
            if a["admitted"] == 0}
        #: chronic honest nulls — appeared DEPRIORITISE_MIN_RUNS times for
        #: this class and admitted nothing, ever, in the window.
        self.late_sources = frozenset(
            name for name, n in self._null_runs.items()
            if n >= DEPRIORITISE_MIN_RUNS)
        #: retrieval-failure-prone — mostly errors when tried. FLAGGED only.
        self.fragile: dict[str, str] = {}
        for name, a in agg.items():
            if a["errored"] >= FRAGILE_MIN_ERRORS and \
                    a["runs"] > 0 and \
                    a["errored"] / a["runs"] >= FRAGILE_MIN_RATE:
                self.fragile[name] = (
                    f"{a['errored']} fetch errors in {a['runs']} "
                    f"{self.question_class}-class run(s)")

    def order_specs(self, specs: list) -> list:
        """ORDER ONLY: stable partition, deprioritised sources last.

        Never drops, adds, or re-ranks within groups. A fully-deprioritised
        candidate list comes back unchanged; sources the memory dislikes
        remain reachable when the budget reaches them.
        """
        return sorted(specs, key=lambda s: getattr(s, "name", "") in
                      self.late_sources)

    def briefing(self) -> str:
        """One diagnostics line for result.notes. Flags, not instructions."""
        if not self.late_sources and not self.fragile:
            return ""
        parts = []
        if self.late_sources:
            parts.append("deprioritised (chronic null for this class): "
                         + ", ".join(sorted(self.late_sources)))
        if self.fragile:
            parts.append("fragile (retrieval failures): "
                         + "; ".join(f"{n} ({r})"
                                     for n, r in sorted(self.fragile.items())))
        return (f"cross-run memory [{self.question_class}, "
                f"{self.runs_considered} prior run(s)]: " + "; ".join(parts)
                + " — ORDER/FLAGS ONLY; informs nothing else")

    def __repr__(self) -> str:
        return (f"PlanningView(class={self.question_class!r}, "
                f"runs={self.runs_considered}, "
                f"late={sorted(self.late_sources)}, "
                f"fragile={sorted(self.fragile)})")


def planning_view(store: CrossRunMemoryStore,
                  question_class: str) -> PlanningView:
    return PlanningView(question_class, store.load_class(question_class))
