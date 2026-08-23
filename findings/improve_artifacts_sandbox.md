# ARTIFACTS, CHARTS AND THE SANDBOX — improvement pass (build/cli-front-door)

**Area chosen: artifacts, charts and the sandbox** (tools/artifacts.py,
tools/sandbox.py, tools/charts.py, and the pipeline seam that consumes them).

Why this one: CLI (×2), retrodiction/calibration, edge sizing, retrieval,
synthesis, checkpointing, provider routing and the schema seam are all taken;
the memory/wiki layer and the source registry have a peer's uncommitted work
in the tree this run, so both were off-limits under exclusive file ownership.
This area is BUILD_MANDATE build-queue item 2 and property 3 ("verifiable,
not voluminous") — and no improve pass has owned it.

## The headline defect — measured

**Every sealed claim citing sandbox output failed its own integrity check.**

`engine._store_sandbox` called `run_python()` (default: workspace deleted on
exit) then `store_sandbox_outputs(..., workspace=None)`. With no workspace,
that function can only create refs from hashes the child reported —
`meta["attested_by_child_only"]=True` — hashes pointing at bytes that were
destroyed milliseconds earlier. Measured before the fix:

    sandbox status: ok  files: [table.csv 81bf9fa8…]
    engine-shape refs → verify_artifacts:
    {'verified': 1, 'missing': ['81bf9fa8…'], 'corrupt': [], 'ok': False}

So property 3's own mechanism — "a claim with the fetched bytes it rests on"
— was broken end to end: the seal covered artifact ids that could never
verify. The gap was even documented as a FINDING test
(test_build_p1_findings.py::test_finding_sandbox_outputs_are_child_attested)
and left as accepted behaviour.

The fix uses machinery that already existed and was never wired:
`keep_workspace=True` (built for exactly this caller). The pipeline now:

1. runs the sandbox with `keep_workspace=True`;
2. seals real bytes — every produced file is independently re-hashed into
   the store;
3. destroys the scratch dir in a `finally` (timeout/error paths safe — the
   workspace attr is absent there and the code degrades to the old
   attested-only fallback).

Measured after (live, this machine):

    verify: {'verified': 2, 'missing': [], 'corrupt': [], 'ok': True}
    workspace removed: True

## Second defect (found by a new test, fixed): store_chart crashed without matplotlib

`store_chart(prefer_matplotlib=False)` raised `UnboundLocalError: data` —
`data` was only bound inside the matplotlib branch. The dependency-free SVG
path, the one this 16 GB box actually uses, had never once executed. Also in
the same function: a dead first `put_json` stored the spec twice (once
without the code), polluting the content-addressed store with a spec
artifact whose regeneration recipe was missing. Both fixed in one hunk.

## What landed (4f1a623)

- `tools/pipeline/engine.py` — keep_workspace + seal-real-bytes + cleanup.
- `tools/charts.py` — single spec artifact carrying the code; SVG path
  actually runs.
- `tests/test_improve_artifacts_sandbox.py` — 8 tests: engine end-to-end
  (seals real bytes AND removes the workspace), child-hash == stored-hash,
  provenance chain intact, error-run seals stdout only, timeout path safe,
  one-spec-not-two, spec carries the code.
- FINDING test updated to FIXED with a pointer to the regression coverage.

## Before/after numbers

| measure | before | after |
|---|---|---|
| sealed sandbox file refs that verify | 0 of N (missing bytes) | all (independently re-hashed) |
| `verify_artifacts` on a claim citing sandbox output | ok: False | ok: True |
| SVG chart path (no matplotlib) | UnboundLocalError, always | works |
| spec artifacts per store_chart call | 2 (one missing code) | 1 (with code) |
| area tests | — | +8 |

Full suite: 2,094 passed, 25 failed. Verified by A/B (stashing only my two
tracked files and re-running) that the w4/w6/redteam failures reproduce
without my diff — they belong to a peer's in-flight retrieval/registry work
uncommitted in this tree, plus the long-documented pre-existing set
(backtest_e2e ×11, claude_findings, prop_scanner, joblib/fastapi collection
errors on this Mac). My area's suites: 77 passed
(b2_artifacts/b2_sandbox/b2_charts/b2_models/p1_pipeline/improve). Sports
untouched.

## What I deliberately did not do

- Did not touch the peer's in-flight files (sources/, retrieval, wiki,
  adversary, hermes_memory) despite their tests failing — exclusive
  ownership held.
- Did not add a "reproduce artifact" command or GC policy for the store —
  nobody asked; `verify_artifacts` and `export_ref` already cover the need.
- Did not change the sandbox's threat model or limits — they are sound and
  tested (env scrub, socket block, seatbelt, rlimits all pass).

## Honest caveats

- The attested-only fallback remains for checkpoint replays where the
  workspace is genuinely gone; it is marked honestly in meta and the FINDING
  test pins its provenance-chain behaviour.
- The peer's uncommitted work was present throughout; my numbers are from
  this tree as found, and the A/B isolates my diff's effect to zero
  regressions.
