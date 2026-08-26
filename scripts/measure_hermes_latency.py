#!/usr/bin/env python3
"""Measure `hermes -z` one-shot latency (fork + model round-trip).

Invokes the same binary the supervisor uses:
    hermes --provider nous -m stealth/ox-alpha -z PONG --in <tmpdir>

Prints elapsed_ms, exit code, and whether stdout contains PONG.
Timeout: 60s per run. Never prints tokens or auth material.
Exit codes: 0 = measured, 2 = hermes/auth missing.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

TIMEOUT_S = 60


def find_hermes():
    path = shutil.which("hermes")
    if not path:
        print("hermes binary not found on PATH", file=sys.stderr)
        sys.exit(2)
    return path


def run_once(hermes_bin, model, provider):
    with tempfile.TemporaryDirectory(prefix="hermes-latency-") as tmpdir:
        cmd = [
            hermes_bin,
            "--provider", provider,
            "-m", model,
            "-z", "PONG",
            "--in", tmpdir,
        ]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
                env={**os.environ, "TERM": "dumb"},
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            stdout = proc.stdout or ""
            stderr_tail = (proc.stderr or "")[-200:]
        except subprocess.TimeoutExpired:
            elapsed_ms = TIMEOUT_S * 1000
            return {"elapsed_ms": elapsed_ms, "exit_code": None,
                    "contains_pong": False, "stderr_tail": "timeout"}
    return {
        "elapsed_ms": elapsed_ms,
        "exit_code": proc.returncode,
        "contains_pong": "PONG" in stdout,
        # keep stderr tail short; should not contain auth but truncate anyway
        "stderr_tail": stderr_tail.replace("\n", " ")[:120],
    }


def main():
    ap = argparse.ArgumentParser(description="Measure hermes -z latency")
    ap.add_argument("--model", default="stealth/ox-alpha")
    ap.add_argument("--provider", default="nous")
    ap.add_argument("-n", type=int, default=1, help="number of runs")
    args = ap.parse_args()

    hermes_bin = find_hermes()
    results = []
    for i in range(args.n):
        r = run_once(hermes_bin, args.model, args.provider)
        results.append(r)
        print(f"run {i + 1}: elapsed_ms={r['elapsed_ms']} "
              f"exit_code={r['exit_code']} pong={r['contains_pong']}")
        if r["exit_code"] not in (0, None) and not r["contains_pong"]:
            print(f"  note: non-zero exit; stderr tail: {r['stderr_tail']}")
    times = sorted(r["elapsed_ms"] for r in results)
    p50 = times[len(times) // 2]
    print(f"summary: n={len(times)} p50_ms={p50} max_ms={times[-1]}")
    print(f"hermes_path={hermes_bin} model={args.model} provider={args.provider}")


if __name__ == "__main__":
    main()
