"""Per-(model, role) empirical score store.

W2 — empirical model routing. The idea: ProviderRouter today maps
task_class -> tier by CONFIGURATION (a human guessed which model suits which
job). tools/retrodiction/ scores research quality against known outcomes, so
model selection can become EMPIRICAL: record which model played which role on
which question and what it scored, then route each role to the model that
measurably does it best.

This module is the RECORD side: an append-only, crash-safe JSONL store keyed
by (role, model). Design constraints:

- Append-only. Never rewrite history; corrections append with a reason.
  Rewriting scores to flatter a model is the same sin as the historical
  signal_generated rewrites the audit found.
- Survives restart. Plain JSON Lines file, one record per line, fsync-free
  (line appends are atomic enough at our sizes; a torn last line is skipped,
  not fatal).
- Queryable. Aggregates (n, mean Brier, mean cost, Wilson-style shrinkage)
  computed from the raw records on demand.
- Honest about basis. Every aggregate carries `n`; callers must be able to
  see whether a routing decision rests on 3 observations or 300.

Scores here are Brier-style losses in [0, 1] where LOWER is better
(0.25 = chance for binary questions), matching tools/retrodiction/scoring.py.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# State files live OFF OneDrive to avoid oplock freezes — same rule as
# tools/state_paths.py. Override with CALLISTO_ROUTING_STATE_DIR; tests pass
# tmp_path explicitly anyway.
def _default_state_dir() -> Path:
    base = os.getenv("CALLISTO_STATE_DIR") or os.path.expanduser(
        "~/.local/state/callisto")
    return Path(os.getenv(
        "CALLISTO_ROUTING_STATE_DIR", str(Path(base) / "routing")))


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ModelScoreStore:
    """Append-only JSONL record of (role, model) outcome scores."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Optional[Path | str] = None):
        if path is None:
            path = _default_state_dir() / "model_scores.jsonl"
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── write ──

    def record(self,
               role: str,
               model: str,
               task_class: str,
               question_id: str,
               brier: float,
               *,
               cost_usd: float = 0.0,
               predicted_probability: Optional[float] = None,
               answer_binary: Optional[bool] = None,
               source: str = "retrodiction",
               notes: str = "") -> dict:
        """Append one scored observation. Returns the stored record.

        `brier` is a loss in [0,1], lower is better. `cost_usd` is what the
        call actually cost (0 for local endpoints) — the store keeps it so the
        ROUTER can trade score against price rather than optimise one.
        """
        if not role or not model or not question_id:
            raise ValueError("role, model and question_id are required")
        if not (0.0 <= float(brier) <= 1.0):
            raise ValueError(f"brier must be in [0,1], got {brier}")
        rec = {
            "v": self.SCHEMA_VERSION,
            "recorded_at": _utcnow_iso(),
            "role": role,
            "task_class": task_class,
            "model": model,
            "question_id": question_id,
            "brier": round(float(brier), 6),
            "cost_usd": round(float(cost_usd), 6),
            "predicted_probability": (
                None if predicted_probability is None
                else round(float(predicted_probability), 6)),
            "answer_binary": answer_binary,
            "source": source,
            "notes": notes,
        }
        line = json.dumps(rec, sort_keys=True)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return rec

    # ── read ──

    def load_all(self) -> list[dict]:
        """All intact records. A torn final line (crash mid-append) is
        skipped, not fatal."""
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
                    if isinstance(rec, dict):
                        out.append(rec)
                except json.JSONDecodeError:
                    continue
        return out

    def records_for(self, role: str, model: str) -> list[dict]:
        return [r for r in self.load_all()
                if r.get("role") == role and r.get("model") == model]

    def models_seen(self, role: Optional[str] = None) -> list[str]:
        seen = {r["model"] for r in self.load_all()
                if role is None or r.get("role") == role}
        return sorted(seen)

    # ── aggregates ──

    @staticmethod
    def aggregate(records: list[dict]) -> Optional[dict]:
        """Aggregate one model's records into a routable summary.

        K2 fix — question identity. Records are DEDUPED on question_id
        (latest row wins): one question recorded 100x is ONE observation,
        not one hundred. Volume must never substitute for breadth, so `n`
        here is distinct questions measured, and the basis labels describe
        breadth of evidence, not row count.

        Shrinkage toward the prior (0.25 = chance) keeps small samples from
        looking heroic: mean_loss is blended with a pseudo-count of PRIOR_N
        chance-level observations. With n=3 the blend dominates; by n=300 the
        data does. This IS the honesty mechanism — a 3-observation mean is not
        allowed to look like a 300-observation mean.
        """
        if not records:
            return None
        PRIOR_N = 10
        PRIOR_LOSS = 0.25
        # Latest record per question_id wins (records arrive in append order).
        by_q: dict[str, dict] = {}
        for r in records:
            by_q[r.get("question_id", "")] = r
        unique = list(by_q.values())
        duplicate_rows = len(records) - len(unique)
        n = len(unique)
        raw_mean = sum(r["brier"] for r in unique) / n
        shrunk = (raw_mean * n + PRIOR_LOSS * PRIOR_N) / (n + PRIOR_N)
        total_cost = sum(float(r.get("cost_usd") or 0.0) for r in unique)
        return {
            "n": n,
            "distinct_questions": n,
            "duplicate_rows_ignored": duplicate_rows,
            "mean_brier_raw": round(raw_mean, 6),
            "mean_brier": round(shrunk, 6),
            "mean_cost_usd": round(total_cost / n, 6),
            "total_cost_usd": round(total_cost, 6),
            "last_recorded_at": max(r.get("recorded_at", "")
                                    for r in records),
        }

    def summary(self, role: str) -> dict:
        """{model: aggregate} for one role, plus honest basis metadata."""
        by_model: dict[str, list[dict]] = {}
        for r in self.load_all():
            if r.get("role") == role:
                by_model.setdefault(r["model"], []).append(r)
        aggs = {}
        for m, recs in by_model.items():
            agg = self.aggregate(recs)
            if agg:
                agg["basis"] = self.basis_label(agg["n"])
                aggs[m] = agg
        return aggs

    # ── honesty ──

    # Basis labels. A routing decision made on 3 observations is NOT the same
    # as one made on 300, and every caller can see which it got.
    BASIS_THRESHOLDS = [
        (30, "measured"),      # >= 30 obs: trust the measurement
        (5, "provisional"),    # 5-29: directionally informative
        (1, "sparse"),         # 1-4: barely better than a guess
    ]

    @classmethod
    def basis_label(cls, n: int) -> str:
        for threshold, label in cls.BASIS_THRESHOLDS:
            if n >= threshold:
                return label
        return "unmeasured"

    def __repr__(self) -> str:
        return (f"ModelScoreStore(path={str(self.path)!r}, "
                f"records={len(self.load_all())})")
