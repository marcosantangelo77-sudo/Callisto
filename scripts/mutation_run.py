#!/usr/bin/env python3
"""Hand-rolled mutation testing harness for Callisto red-team.

Applies one operator at a time to a target file, runs a TARGETED pytest subset,
records SURVIVED / KILLED, and restores the original file bytes after every
mutant (and in a finally block). Never commits anything.

Usage: python3 scripts/mutation_run.py
Results: findings/mutation_results.json + stdout table.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []
BACKUP_SUFFIX = ".mutation_backup"

# file -> targeted test selection (pytest args)
TARGETS = {
    "agp/thresholds.py": [
        "tests/test_confidence.py", "tests/test_agp.py", "tests/test_agp_seal.py",
        "tests/test_a6_seal_gate.py", "tests/test_redteam_confidence_inflation.py",
        "tests/test_redteam_confidence_laundering.py", "tests/test_fix_ckpt_confidence.py",
        "tests/test_tier3_epi_provenance.py",
        "--deselect=tests/test_redteam_confidence_laundering.py::TestCitationLaundering::test_synthesis_best_class_laundering_in_group",
        "--deselect=tests/test_redteam_confidence_laundering.py::TestSelfReviewEscape::test_panel_verdict_blocking_veto_returns_rounded_up_score",
    ],
    "agp/provenance.py": [
        "tests/test_tier3_epi_provenance.py", "tests/test_agp_seal.py",
        "tests/test_redteam_prov_memory_wiki.py", "tests/test_confidence.py",
        "tests/test_a6_seal_gate.py", "tests/test_build_p2_claims.py",
    ],
    "agp/adversary.py": [
        "tests/test_build_r3_adversary.py", "tests/test_agp.py",
        "tests/test_redteam_c3_vacuous_guard.py",
    ],
    "tools/pipeline/retrieval.py": [
        "tests/test_build_w1_retrieval.py", "tests/test_redteam_retrieval_relevance.py",
        "tests/test_hypgen_retrieval.py", "tests/test_build_p1_pipeline.py",
        "--deselect=tests/test_redteam_retrieval_relevance.py::test_r2_three_char_prefix_junk_reaches_88pct_coverage",
        "--deselect=tests/test_redteam_retrieval_relevance.py::test_r2b_short_question_one_common_word_admits_anything_containing_it",
        "--deselect=tests/test_redteam_retrieval_relevance.py::test_r3_engine_fallback_counts_family_members_as_two_voices",
        "--deselect=tests/test_redteam_retrieval_relevance.py::test_r3b_sandbox_success_adds_a_fake_independent_voice",
        "--deselect=tests/test_redteam_retrieval_relevance.py::test_r4_gate_rejected_bytes_still_read_as_primary_in_ledger",
        "--deselect=tests/test_redteam_retrieval_relevance.py::test_r4b_gate_rejected_url_still_verifies_citations",
    ],
    "tools/kelly.py": [
        "tests/test_tier0_money_kelly.py", "tests/test_redteam_money_path.py",
        "tests/test_portfolio_sizing.py", "tests/test_bankroll_sim.py",
        "tests/test_bankroll_race.py", "tests/test_drawdown_kill.py",
    ],
    "tools/edge_confidence.py": [
        "tests/test_edge_confidence.py", "tests/test_confidence.py",
        "tests/test_redteam_money_path.py",
    ],
}


def mutations_for(path):
    """Yield (lineno, description, apply_fn) triples via AST scan."""
    src = open(os.path.join(REPO, path)).read()
    tree = ast.parse(src)
    out = []

    def add(node, desc, repl_src):
        out.append((node.lineno, desc, repl_src))

    CMP_SWAPS = {
        ast.Lt: ">=", ast.LtE: ">", ast.Gt: "<=", ast.GtE: "<",
        ast.Eq: "!=", ast.NotEq: "==", ast.Is: "is not", ast.IsNot: "is",
        ast.In: "not in", ast.NotIn: "in",
    }
    ARITH_SWAPS = {ast.Add: "-", ast.Sub: "+", ast.Mult: "/", ast.Div: "*"}
    BOOL_SWAPS = {"and": "or", "or": "and"}

    class V(ast.NodeVisitor):
        def visit_Compare(self, node):
            for i, op in enumerate(node.ops):
                if type(op) in CMP_SWAPS:
                    add(node, f"compare op #{i+1} -> {CMP_SWAPS[type(op)]}",
                        None)  # handled textually below
            self.generic_visit(node)

        def visit_BinOp(self, node):
            if type(node.op) in ARITH_SWAPS:
                add(node, f"{type(node.op).__name__} -> {ARITH_SWAPS[type(node.op)]}", None)
            self.generic_visit(node)

        def visit_BoolOp(self, node):
            opv = "and" if isinstance(node.op, ast.And) else "or"
            add(node, f"{opv} -> {BOOL_SWAPS[opv]}", None)
            self.generic_visit(node)

        def visit_Call(self, node):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name == "min":
                add(node, "min -> max", None)
            elif name == "max":
                add(node, "max -> min", None)
            elif name == "floor":
                add(node, "floor -> round", None)
            elif name == "round":
                add(node, "round -> floor", None)
            self.generic_visit(node)

        def visit_Constant(self, node):
            if isinstance(node.value, bool):
                return
            if isinstance(node.value, (int, float)) and node.value != 0:
                if isinstance(node.value, int):
                    v = node.value + 1
                    vs = str(v)
                else:
                    v = node.value * 1.1
                    vs = f"{v:.10g}"
                add(node, f"constant {node.value!r} -> {vs}", None)

    V().visit(tree)
    return src, out


# textual single-occurrence replacements keyed by (lineno, description-prefix).
# We implement application by regenerating the line with a regex on the exact
# construct; fall back to skipping mutants we cannot apply unambiguously.
import re

def apply_mutation(src, lineno, desc):
    lines = src.split("\n")
    idx = lineno - 1
    if idx >= len(lines):
        return None
    line = lines[idx]
    new = None
    m = re.match(r"compare op #(\d+) -> (.+)", desc)
    if m:
        n, repl = int(m.group(1)), m.group(2)
        # replace nth comparison operator on the line (approximate: count ops)
        ops = {
            ">=": "<=", "<=": ">=", ">": "<", "<": ">",
            "==": "!=", "!=": "==", " is not ": " is ", " is ": " is not ",
            " not in ": " in ", " in ": " not in ",
        }
        # find all candidate operator tokens with positions
        tokens = []
        for pat in [r">=", r"<=", r"==", r"!=", r"(?<![<>=!])>(?!=)", r"(?<![<>=!])<(?!=)"]:
            for mm in re.finditer(pat, line):
                tokens.append(mm.span())
        tokens.sort()
        # merge overlapping (e.g. >= matched by > pattern) — prefer longest leftmost
        merged = []
        for s, e in sorted(tokens):
            if merged and s < merged[-1][1]:
                continue
            merged.append((s, e))
        if n - 1 >= len(merged):
            return None
        s, e = merged[n - 1]
        old = line[s:e]
        new = line[:s] + ops[old] + line[e:]
    elif "min -> max" in desc:
        new = re.sub(r"\bmin\(", "max(", line, count=1)
    elif "max -> min" in desc:
        new = re.sub(r"\bmax\(", "min(", line, count=1)
    elif "floor -> round" in desc:
        new = re.sub(r"\bfloor\(", "round(", line, count=1)
    elif "round -> floor" in desc:
        new = re.sub(r"\bround\(", "floor(", line, count=1)
    else:
        m = re.match(r"(Add|Sub|Mult|Div) -> (\w+)", desc)
        if m:
            rsym = {"Add": "-", "Sub": "+", "Mult": "/", "Div": "*"}[m.group(2)]
            sym = {"Add": "+", "Sub": "-", "Mult": "*", "Div": "/"}[m.group(1)]
            # replace first occurrence of the symbol outside of comparisons like <= >= !=
            for i, ch in enumerate(line):
                if ch == sym:
                    prev = line[i - 1] if i else ""
                    nxt = line[i + 1] if i + 1 < len(line) else ""
                    if prev in "<>!=" or nxt == "=":
                        continue
                    new = line[:i] + rsym + line[i + 1:]
                    break
        else:
            m = re.match(r"constant (\S+) -> (\S+)", desc)
            if m:
                old_v, new_v = m.group(1), m.group(2)
                # word-boundary replace, avoid matching inside identifiers
                pat = r"(?<![\w.])" + re.escape(old_v) + r"(?![\w.])"
                new = re.sub(pat, new_v, line, count=1)
            elif "-> and" in desc:
                new = re.sub(r"\band\b", "or", line, count=1)
            elif "-> or" in desc:
                new = re.sub(r"\bor\b", "and", line, count=1)
    if new is None or new == line:
        return None
    # sanity: mutated source must parse
    trial = "\n".join(lines[:idx] + [new] + lines[idx + 1:])
    try:
        ast.parse(trial)
    except SyntaxError:
        return None
    return trial


def run_tests(target_files, timeout):
    env = dict(os.environ)
    env["CALLISTO_ENABLE_NETWORK"] = "0"
    env["CALLISTO_SOURCE_HEALTH_NET"] = "0"
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--no-header",
           "-p", "no:cacheprovider"] + target_files
    try:
        p = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout[-2000:]
    except subprocess.TimeoutExpired:
        return 99, "TIMEOUT"


def main():
    only = sys.argv[1:] or list(TARGETS)
    t0 = time.time()
    try:
        for rel in only:
            src, muts = mutations_for(rel)
            full = os.path.join(REPO, rel)
            backup = full + BACKUP_SUFFIX
            tests = TARGETS[rel]
            print(f"\n=== {rel}: {len(muts)} mutants, {len(tests)} test files ===", flush=True)
            # baseline
            rc, _ = run_tests(tests, 600)
            baseline_ok = rc == 0
            print(f"baseline rc={rc} ok={baseline_ok}")
            if not baseline_ok:
                RESULTS.append({"file": rel, "error": f"baseline failing rc={rc}"})
                continue
            applied = 0
            for lineno, desc, _ in muts:
                mutated = apply_mutation(src, lineno, desc)
                if mutated is None:
                    continue
                applied += 1
                with open(full, "w") as f:
                    f.write(mutated)
                try:
                    rc, tail = run_tests(tests, 600)
                    status = "KILLED" if rc != 0 else ("TIMEOUT-SURVIVED" if rc == 99 else "SURVIVED")
                    rec = {"file": rel, "line": lineno, "mutation": desc,
                           "status": status, "pytest_tail": tail if status == "SURVIVED" else ""}
                    RESULTS.append(rec)
                    print(f"[{status}] L{lineno} {desc}", flush=True)
                finally:
                    shutil.copyfile(backup, full) if os.path.exists(backup) else open(full, "w").close() if False else None
                    with open(full, "w") as f:
                        f.write(src)
            print(f"applied {applied}/{len(muts)} mutants for {rel}")
    finally:
        # hard restore everything from backup copies
        for rel in TARGETS:
            full = os.path.join(REPO, rel)
            bak = full + BACKUP_SUFFIX
            if os.path.exists(bak):
                shutil.copyfile(bak, full)
                os.remove(bak)
        os.makedirs(os.path.join(REPO, "findings"), exist_ok=True)
        with open(os.path.join(REPO, "findings/mutation_results.json"), "w") as f:
            json.dump(RESULTS, f, indent=1)
    surv = [r for r in RESULTS if r.get("status") == "SURVIVED"]
    killed = sum(1 for r in RESULTS if r.get("status") == "KILLED")
    print(f"\nDONE in {time.time()-t0:.0f}s: {killed} killed, {len(surv)} SURVIVED")
    for r in surv:
        print(f"  SURVIVED {r['file']}:{r['line']} {r['mutation']}")


if __name__ == "__main__":
    main()
