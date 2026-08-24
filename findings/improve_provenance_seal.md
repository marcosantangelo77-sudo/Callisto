# IMPROVE — provenance / seal seam (2026-08-24, ox-alpha)

**Area:** the provenance ledger ↔ relevance gate ↔ seal boundary
(`agp/provenance.py`, `tools/pipeline/engine.py` seal block).

Why this one: artifacts/sandbox, CLI (twice), retrodiction/calibration and edge
sizing all have improve runs. The provenance layer is the system's core claim —
"confidence assigned by which code path fetched the bytes" — and no improve run
had ever attacked the *seam* between the gate's reject verdict and the ledger.
PATTERNS.md family 1 ("a check that cannot fail") predicted exactly this spot.

**Family hunted: #1** (verification that never runs / a check whose input is
missing silently passes). Two instances found; both fixed with repro tests.

---

## Defect P — gate rejection LAUNDERS provenance across URLs

`ProvenanceLedger.record_gate_rejection(content, urls)` popped the content
hash from `_by_hash` outright. But identical bytes are routinely served at two
URLs (same wire story syndicated; in the live test fixture, the same OpenAlex
body answering two different leaf queries). When ONE of those fetches was
gate-rejected:

- the rejection ERASED the PRIMARY observation of the admitted sibling fetch;
- `is_primary_bytes(admitted_body)` went False for evidence the pipeline had
  actually sealed on;
- source-class ceilings silently dropped.

Measured consequence: `tests/test_build_p1_pipeline.py::
test_end_to_end_sealed_with_provenance_artifact_and_adversary` FAILED on the
pre-change tree (openalex fetch "not in ledger as primary bytes") — a red-team
repro test from the R4/R4b fix itself was being violated by its own remedy.

    before: rejected_fetches_noted golden encoded 3 observations
            (the rejection deleted an observation from the audit trail)
    after:  4 observations retained; rejected URL still verifies nothing;
            is_primary_bytes(rejected bytes) still False (R4/R4b preserved)

Fix (921bd9d): rejection is judged PER-URL. `_fully_rejected(hash)` returns
True only when every URL observation of these bytes was rejected. Late replays
of fully-rejected bytes are still refused; the rejected URL still verifies no
citation. The failure direction is unchanged (only downward), but it can no
longer spill onto innocent sibling observations.

## Defect Q — A20's seal-over-artifacts layer was INERT in production

The A20 red-team fix added `AGPSession.artifact_refs`, `add_artifacts()`, and
serialized the layer into the keyed-HMAC seal payload. Family-1 check: who
calls it? **Nobody. Zero production callers, zero test callers.** The engine
kept refs on itself (`self.artifact_refs`), gated them, attached them to
`PipelineResult` only AFTER `session.seal()` — so every seal ever minted
hashed an EMPTY artifact layer while the module comment promised the seal
"covers the quantitative artifacts".

Measured consequence: for any sealed run with sandbox outputs,
`session.to_dict()["artifact_refs"] == []` while
`result.artifact_refs != []`. `verify_seal` verified less than advertised —
exactly the W5/C1/A6 shape: machinery that looks authoritative and never runs.

Fix (landed via autosave bc485b3/4c426c6, same content as my working tree):
engine attaches `self.artifact_refs` to the session immediately before
`session.seal()`. Behavioral test runs a compute pipeline end-to-end and
asserts the sealed payload carries the refs.

## Verification

- New suite `tests/test_improve_provenance_seal.py`: 4 tests, all failing
  before each fix (mutation-checked), passing after.
- Full suite before change: 31 failures (30 pre-existing + the e2e test my
  defect P broke). After: 30 — every remaining failure reproduced identically
  on the pre-change parent commit (money-path M1-M6, S1-S4, review-run5,
  tier3 veto/trust, etc. — owned by other instances, untouched here).
- Golden update: `speed_golden/rejected_fetches_noted.json` now records the
  corrected ledger semantics; fingerprint predicate reads PRIMARY via the
  public query API rather than raw internals.

## Left on the table (next run's candidates)

- `agp/research_program.py.ArtifactRef` has no kind vocabulary and no store
  binding; `ResearchProgram.artifacts` has zero writers.
- `ModelRegistry.artifact_refs` stores sha strings but never re-hashes them
  against the store — another dormant verification surface.
- The 30 pre-existing failures above include the entire money-path repro
  suite failing on this branch (review run 12's finding, still unmerged).
