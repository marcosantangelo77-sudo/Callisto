#!/usr/bin/env python3
"""Hand-rolled mutation harness for Callisto red-team pass.

Applies one mutation at a time to a production file, runs a targeted test
subset, records survive/kill (relative to a pre-recorded baseline of
already-failing tests), restores the file, and verifies restoration byte
for byte. Never leaves a mutant on disk across runs.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # scripts/ -> repo root

# file -> targeted tests (scoped, not the full suite)
TEST_MAP = {
    "agp/thresholds.py": [
        "tests/test_redteam_confidence_laundering.py",
        "tests/test_redteam_confidence_inflation.py",
        "tests/test_build_i3_synthesis.py",
    ],
    "agp/provenance.py": [
        "tests/test_tier3_epi_provenance.py",
        "tests/test_redteam_confidence_laundering.py",
        "tests/test_redteam_confidence_inflation.py",
        "tests/test_build_w1_retrieval.py",
        "tests/test_build_information_gain.py",
        "tests/test_stasis_stop.py",
    ],
    "agp/adversary.py": [
        "tests/test_build_r3_adversary.py",
        "tests/test_build_human_critic.py",
        "tests/test_redteam_confidence_laundering.py::TestSelfReviewEscape",
        "tests/test_redteam_confidence_inflation.py",
        "tests/test_build_w4_cross_model.py",
    ],
    "tools/pipeline/retrieval.py": [
        "tests/test_build_w1_retrieval.py",
        "tests/test_redteam_retrieval_relevance.py",
        "tests/test_build_information_gain.py",
        "tests/test_stasis_stop.py",
        "tests/test_speed_parallel_sources.py",
    ],
    "tools/kelly.py": [
        "tests/test_tier0_money_kelly.py",
        "tests/test_tier0_money_sizing_and_units.py",
        "tests/test_bankroll_sim.py",
        "tests/test_bankroll_race.py",
        "tests/test_portfolio_sizing.py",
        "tests/test_clv_paper_trades.py",
    ],
    "tools/edge_confidence.py": [
        "tests/test_edge_confidence.py",
        "tests/test_full_system_audit.py::TestEdgeConfidence" if False else "tests/test_edge_confidence.py",
    ],
}

PY = sys.executable


def run_tests(tests):
    cmd = [PY, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
           "--no-summary"] + list(tests)
    # start_new_session so we can kill the whole pytest process group on hang
    p = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        import signal, os
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.communicate()
        raise
    failed = set()
    for line in (out + err).splitlines():
        if line.startswith("FAILED"):
            failed.add(line.split()[1])
    return p.returncode != 0, sorted(failed)


def build_baseline():
    baseline_file = REPO / ".mutation_baseline.json"
    if baseline_file.exists():
        return json.loads(baseline_file.read_text())
    base = {}
    for f, tests in TEST_MAP.items():
        _, failed = run_tests(tests)
        base[f] = failed
        print(f"baseline {f}: {len(failed)} pre-failing")
    baseline_file.write_text(json.dumps(base))
    return base


import ast
import io
import tokenize

BOUNDARY_SWAPS = [("<=", "<"), (">=", ">")]
COMPARISONS = [("<=", ">="), (">=", "<="), ("==", "!="), ("<", ">"), (">", "<")]


def _code_spans(src):
    """{(row, start_col, end_col)} for real code tokens (not strings/comments)."""
    spans = set()
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return None  # unparsable: mutate nothing
    for tok in toks:
        if tok.type in (tokenize.STRING, tokenize.COMMENT,
                        tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                        tokenize.DEDENT, tokenize.ENDMARKER):
            continue
        row, c1, c2 = tok.start[0], tok.start[1], tok.end[1]
        if tok.string.strip():
            spans.add((row, c1, c2))
    return spans


def gen_mutants(src):
    """Yield (line_no_1based, description, mutated_line) — one token changed.
    Only mutates positions inside real code tokens; strings/comments immune."""
    lines = src.split("\n")
    spans = _code_spans(src)
    if spans is None:
        return []

    def in_code(lineno, col):
        return any(r == lineno and c1 <= col < c2 for r, c1, c2 in spans)

    mutants = []
    for idx, line in enumerate(lines):
        ln = idx + 1
        stripped = line.strip()
        if not stripped:
            continue
        cands = []

        def add_if_code(desc, s, cols):
            if all(in_code(ln, c) for c in cols):
                cands.append((desc, s))

        for m in re.finditer(r"\bmin\b", line):
            s = line[:m.start()] + "max" + line[m.end():]
            add_if_code("min->max", s, range(m.start(), m.end()))
        for m in re.finditer(r"\bmax\b", line):
            s = line[:m.start()] + "min" + line[m.end():]
            add_if_code("max->min", s, range(m.start(), m.end()))
        for old, new in BOUNDARY_SWAPS + COMPARISONS:
            pos = 0
            while True:
                i = line.find(old, pos)
                if i < 0:
                    break
                s = line[:i] + new + line[i + len(old):]
                # comparison tokens: single OP token covering the chars
                add_if_code(f"{old}->{new}", s, range(i, i + len(old)))
                pos = i + len(old)
        # constant tweaks
        for m in re.finditer(r"(?<![\w.])(\d+\.\d+)(?![\w.])", line):
            fv = m.group(1)
            nv = f"{float(fv) + 0.01:.2f}"
            mutated = line[:m.start(1)] + nv + line[m.end(1):]
            add_if_code(f"const {fv}->{nv}", mutated, range(m.start(1), m.end(1)))
        for m in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line):
            v = int(m.group(1))
            nv = 1 if v == 0 else v + 1
            mutated = line[:m.start(1)] + str(nv) + line[m.end(1):]
            add_if_code(f"const {v}->{nv}", mutated, range(m.start(1), m.end(1)))
        # rounding-direction swaps (the subtraction-only invariant lives here)
        for old, new in (("floor", "round"), ("floor", "ceil"), ("round", "floor")):
            pos = 0
            while True:
                i = line.find(old, pos)
                if i < 0:
                    break
                s = line[:i] + new + line[i + len(old):]
                add_if_code(f"{old}->{new}", s, range(i, i + len(old)))
                pos = i + len(old)
        seen = set()
        for desc, s in cands:
            if s == line:
                continue
            k = (desc, s)
            if k in seen:
                continue
            seen.add(k)
            mutants.append((ln, desc, s))
    return mutants


def main():
    targets = sys.argv[1:] or list(TEST_MAP)
    # pre-flight: every target must be clean vs HEAD, else 'original' could be
    # a leftover mutant (the autosave daemon has committed mutants mid-run
    # in this repo before).
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--"]
                           + targets, capture_output=True, text=True).stdout.strip()
    if dirty:
        raise SystemExit("REFUSING TO RUN: target files dirty vs HEAD:\n" + dirty)
    baseline = build_baseline()
    results = []
    for rel in targets:
        path = REPO / rel
        original = path.read_bytes()
        # on-disk pristine backup: immune to in-process state corruption
        import tempfile, shutil
        backup = Path(tempfile.mkstemp(suffix=".bak")[1])
        backup.write_bytes(original)
        tests = TEST_MAP[rel]
        t0 = time.time()
        n_run = n_kill = n_survive = n_err = 0
        survivors = []
        for lineno, desc, mutline in gen_mutants(original.decode()):
            src_lines = original.decode().split("\n")
            src_lines[lineno - 1] = mutline
            full = "\n".join(src_lines)
            path.write_text(full, newline="")
            # quick sanity: file must still parse
            import ast
            try:
                ast.parse(path.read_text())
            except SyntaxError:
                path.write_bytes(original)
                n_err += 1
                continue
            n_run += 1
            try:
                _, failed_now = run_tests(tests)
            except subprocess.TimeoutExpired:
                path.write_bytes(backup.read_bytes())
                failed_now = {"<timeout>"}
            killed = bool(set(failed_now) - set(baseline.get(rel, [])))
            if killed:
                n_kill += 1
            else:
                n_survive += 1
                survivors.append({"file": rel, "line": lineno,
                                  "mutation": desc,
                                  "mutated": mutline.strip()})
            if n_run % 25 == 0:
                print(f"  [{rel}] {n_run} run, {n_kill} killed, {n_survive} survive "
                      f"({time.time()-t0:.0f}s)", flush=True)
        # verify clean restore
        path.write_bytes(backup.read_bytes())
        if path.read_bytes() != backup.read_bytes():
            raise AssertionError(f"RESTORE FAILURE: {rel}")
        backup.unlink()
        print(f"DONE {rel}: {n_run} mutants, {n_kill} killed, {n_survive} SURVIVED, "
              f"{n_err} syntax-skipped, restore verified ({time.time()-t0:.0f}s)", flush=True)
        results.extend(survivors)
    out = REPO / "findings" / "mutation_survivors_raw.json"
    out.write_text(json.dumps(results, indent=1))
    print(f"wrote {out} ({len(results)} survivors)")


if __name__ == "__main__":
    main()
