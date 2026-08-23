# FAMILY-1 HUNT IN THE ARTIFACT CHAIN — improvement pass (review/ox-alpha-0823)

**Family hunted: #1 — "A verification layer that never actually runs."**
Method per PATTERNS.md: for every verification layer, ask what calls it and
what happens when its input is MISSING. Also applied family 3 (absence as
success) and 7 (mutation-verify the tests).

Area overlap note: this run's home worktree (review-ox) had already produced
improve passes for artifacts/sandbox and CLI. This pass is NOT a re-run of
those — it hunts the FAMILY across the artifact verification chain, which no
prior pass did, and found two more instances of it.

## The headline: `verify_artifacts()` still has ZERO production callers

The improve_artifacts_sandbox run fixed how bytes get INTO the store (A6:
child-attested-only refs) and its tests call `verify_artifacts` directly —
but production code still never does. Grep across tools/, agp/, api.py,
callisto.py: the only non-test reference is the function's own docstring.
The exact shape of W5/K1/C1/A6: a check that exists, looks authoritative,
and nothing invokes.

Consequence: a pipeline run could seal a conclusion whose `artifact_refs`
pointed at bytes that were never stored, or stored and later corrupted.
The ONLY verification anywhere in the chain was `callisto show`, a manual,
post-hoc CLI convenience. Property 3 ("evidence a human can check") was
enforced by nobody-checks-unless-they-feel-like-it.

### Fix A7 (commit 7e75b76)

- `AGPSession.artifact_check`: a new fail-closed seal gate, mirroring the
  existing `seal_veto` hook exactly — callable(session) → str; empty = all
  cited artifacts verified; non-empty = refusal reason; crash = refuse
  (fail closed). Enforced inside `seal()`, before the reviewer veto.
- `ResearchPipeline.run` installs it whenever `self.artifact_refs` is
  non-empty (i.e. the run's sandbox compute produced artifacts), closing
  over the RUN'S OWN store — not the process-global default store, which
  was the first implementation's bug and is why the hook takes the check,
  not bare refs (agp stays storage-agnostic).
- This makes `verify_artifacts()`'s first production caller the seal path
  itself: an un-verifiable artifact now blocks sealing instead of riding
  into a sealed claim.

## Second instance: the retrodiction batch drops the artifact chain (A8)

`RetrodictionBatch._run_one` enriches each scored row from the pipeline
result — sealed, refusal_reason, n_fetches, objections, notes — but not
`artifact_refs`. The batch JSONL IS the system's scored track record
(NEXT.md §1: "the only honest way to compare synthesis strategies"). A
scored row said "Brier 0.19" with no way to re-check what that conclusion
was backed by. Family-1 shape again: the evidence exists one field away and
the record simply never carried it.

Fix: `BatchResult.artifacts` (kind/sha256/name dicts), populated in
`_run_one`, persisted in every row.

## Verification

- 8 new tests in tests/test_family1_artifact_gate.py: missing ref refuses,
  corrupt bytes refuse, intact seals, crash fails closed, legacy sessions
  unchanged, engine wiring present on a real compute-producing run, batch
  row carries refs.
- Mutation check (PATTERNS #7): disabling the gate in agp/__init__.py makes
  3 of those tests FAIL; restored, all pass. The gate is load-bearing.
- One stale characterization test updated per its own docstring rule
  ("if these fail because the code changed, update test + DEEP_RESEARCH
  claim together"): test_tier7_deepresearch asserted `"artifact" not in
  agp/__init__.py` repo-wide — falsified by the gate itself. Rewritten to
  assert what actually matters now: SessionSummary carries no artifact
  claims, the gate exists.
- Full suite: 11,154 passed, 9 skipped. 10 failures verified PRE-EXISTING
  by stashing this diff and re-running the same tests on the clean tree
  (lifecycle_claim ×2, prop_scanner ×1, redteam_laundering ×2,
  review_2026_08_23 ×5 — the R1/R2/R3 half-landed items from ed1cc34).

## What I deliberately did NOT do

- Did not add artifact checks to AGPSession.to_dict / the sealed payload —
  the refs are already hash-covered via the CLI run records; adding them to
  the canonical payload would break verify_seal against existing sealed
  rows for zero epistemic gain.
- Did not touch charts/workbook paths — prior pass covered them; their gap
  (no pipeline caller for store_chart/build_workbook) is real but is a
  capability question, not a family-1 defect.

## Remaining family-1 surface (for the next hunter)

- `tools/model_registry.py` also declares `artifact_refs` — not audited here.
- The CLI `_verify_artifact` duplicates hashing logic rather than calling
  `store.verify_artifacts`; harmless today, but family #2 says duplicated
  verifiers drift.
