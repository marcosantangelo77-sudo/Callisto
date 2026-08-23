# ARTIFACTS, CHARTS AND THE SANDBOX — improvement pass (build/artifacts-sandbox)

**Area chosen: artifacts, charts and the sandbox** — tools/sandbox.py,
tools/artifacts.py, tools/charts.py, tools/model_registry.py, plus the
pipeline seam (tools/pipeline/engine.py compute stage) that consumes them.

Why this one: BUILD_MANDATE item 2 ("compute sandbox + artifact store" —
property 3, verifiable not voluminous) and no improve run has owned it. CLI
was done twice; retrodiction and edge quantification in the last two runs;
retrieval/synthesis/routing/schema/checkpointing before that.

## What was wrong — measured

**1. The headline defect: sandbox-produced file bytes never reached the
artifact store.** The pipeline's compute stage called `run_python(...)` with
the default `keep_workspace=False`, which destroys the scratch dir, then
called `store_sandbox_outputs(sbx, store, workspace=None)`. In that mode the
store can only record the hashes the child *claimed* for its files, marked
`meta["attested_by_child_only"]=True`. Measured consequence, reproduced on
the real pipeline with a leaf that computes and writes a file:

    before: out.json → bytes_in_store: False, verify_artifacts → missing
            (a sealed claim cited an artifact nobody could re-check)

That is property 3 inverted: the evidence was *citable* but not
*checkable*. The defect was documented honestly as FINDING #3 in
findings/instance-p1.md but never fixed. No test caught it because the
existing compute-stage test's fixture code writes no file — only stdout and
a return value, which were always stored fine.

**2. `charts.store_chart` mutated its caller's spec dict.** It did
`spec.pop("code")` in place (and put_json'd twice, wasting one write). Any
caller reusing a spec after storing it silently lost its regeneration code
— the exact payload that makes "a chart you cannot regenerate from its spec
is prose" false. Verified by inspection + new pinning test.

**3. Stale-workspace leak risk noted, not introduced:** three
`callisto_sbx_*` dirs dated ~28h before this session sat in $TMPDIR,
evidence an earlier run leaked preserved workspaces (keep_workspace with no
cleanup). The new engine path destroys the workspace immediately after
sealing; a regression test pins that nothing newer than run-start survives.

## What changed

- **engine.py**: compute stage runs `run_python(..., keep_workspace=True)`;
  after `_store_sandbox` seals real bytes, `_cleanup_workspace` removes the
  scratch dir. Failure/timeout paths unchanged (nothing to seal).
- **charts.py**: spec stored via `{**spec}` copy; caller's dict untouched;
  duplicate put_json removed.
- **tests/test_build_artifacts_sandbox_improve.py** (5 tests): file bytes
  present AND verifiable through the real pipeline; zero attested-only refs;
  workspace cleanup; store_chart non-mutation (incl. identical chart hash on
  spec reuse); all offline.
- **tests/test_build_p1_findings.py**: the attested-only FINDING test
  docstring updated to RESOLVED-at-the-seam (its fallback-path assertions
  still hold for direct callers who pass workspace=None).

Deliberately NOT done:
- No change to `store_sandbox_outputs`'s honest attested-fallback — direct
  callers without a workspace keep truthful behaviour.
- No change to model_registry (its Brier/reliability scoring is sound and
  tested; the sandbox→registry chain test already passes).
- Did not adopt an external sandbox library: the threat model (prompt-
  injected LLM code on the owner's own machine) is defense-in-depth, and
  macOS seatbelt + rlimits + env scrub is the right weight; heavy VM-style
  isolation would add capability nobody asked for.

## Before/after

| measure | before | after |
|---|---|---|
| pipeline sandbox file artifacts with bytes in store | 0 of N files (attested-only, unverifiable) | all files stored & re-hashed |
| verify_artifacts over pipeline artifact_refs | reported missing for file refs | ok |
| caller's chart spec after store_chart | 'code' popped | unchanged |
| area tests | — | +5 |
| full suite (this Mac) | 2041 passed / 6 failed / 4 errors* | same failures, none new |

\* pre-existing set: adaptive_timeout ×4, claude_findings, prop_scanner;
errors are fastapi/joblib/xgboost import gaps; backtest_e2e ×11 verified
failing identically on the shared checkout before my changes. Sports/money
suites green (hypothesis, promotion gates, tier0 kelly, clv units, r5 edge,
b1 clv gate: 98 passed).

## Honest caveats

- The cleanup test uses an mtime window; on a machine running concurrent
  Callisto sandboxes at test time it could theoretically see another
  process's fresh dir. Snapshot-based assertion was tried and rejected for
  exactly the opposite reason — stale dirs from other instances.
- keep_workspace=True means the scratch dir briefly exists past process
  exit; contents are still seatbelt-confined and destroyed post-seal.
- ProvenanceLedger durability (instance-p1 finding #4) remains open — not
  this area's file.
