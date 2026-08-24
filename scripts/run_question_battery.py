#!/usr/bin/env python3
"""Question battery runner — known-answer testing at scale.

Drives `callisto.py ask --backend ox_alpha` one question at a time,
records per-question results incrementally to findings/battery/results.jsonl
(one JSON line per completed question, safe against provider outages),
and never modifies the pipeline.

Usage:
    python3 scripts/run_question_battery.py            # runs all remaining
    python3 scripts/run_question_battery.py 5          # runs next 5 only
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QUESTIONS = Path(__file__).resolve().parent.parent / "findings" / "battery" / "questions.json"
RESULTS = Path(__file__).resolve().parent.parent / "findings" / "battery" / "results.jsonl"


def load_done() -> set[str]:
    done = set()
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["id"])
            except Exception:
                pass
    return done


def run_one(q: str) -> dict:
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(REPO / "callisto.py"), "ask", "--backend", "ox_alpha", q],
        capture_output=True, text=True, timeout=1500, cwd=str(REPO),
    )
    elapsed = round(time.monotonic() - t0, 1)
    rec = {
        "question": q,
        "elapsed_s": elapsed,
        "rc": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-800:],
        "run_record": None,
    }
    # pull the persisted run-record path out of stdout
    for line in proc.stdout.splitlines():
        if line.startswith("run      : ") and line.endswith(".json"):
            rec["run_record"] = line.split(":", 1)[1].strip()
    return rec


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    bank = json.loads(QUESTIONS.read_text())
    done = load_done()
    todo = [q for q in bank if q["id"] not in done]
    print(f"{len(done)} done, {len(todo)} remaining")
    if limit:
        todo = todo[:limit]
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    for i, q in enumerate(todo):
        print(f"[{i+1}/{len(todo)}] {q['id']}: {q['question'][:90]}", flush=True)
        try:
            rec = run_one(q["question"])
        except subprocess.TimeoutExpired:
            rec = {"question": q["question"], "elapsed_s": None, "rc": "timeout",
                   "stdout_tail": "", "stderr_tail": "", "run_record": None}
        rec["id"] = q["id"]
        with RESULTS.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        tail = rec.get("stdout_tail", "")
        verdict = "SEALED" if "SEALED" in tail else ("REFUSED" if "REFUSED" in tail else "?")
        print(f"    -> {verdict} ({rec.get('elapsed_s')}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
