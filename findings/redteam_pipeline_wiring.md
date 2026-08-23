# RED TEAM — PIPELINE WIRING & THE RETRODICTION FEEDBACK PATH

**Surface choice:** six prior passes attacked components (confidence,
loop incentives, provenance/checkpoint boundaries, retrieval/independence,
sealing/preregistration, synthesis/corroboration) and one attacked the
calibration scorer itself (`tests/test_redteam_calib_scoring.py`, untracked
on this branch — left untouched, its K-findings not duplicated here). This
pass attacks the SEAMS: how `engine.run` composes clamps and tiers across
stages, what the resume boundary trusts, who the retrodiction batch believes,
and what the routing store does with what it is fed. The working hypothesis:
a component that is individually sound can still inflate a number by handing
it to a neighbour one stage too early or at one trust level too high. That
hypothesis was right eight times.

Tests: `tests/test_redteam_pipeline_wiring.py` — 12 passing (8 defect
canaries + 4 honest-negative pins). Canaries FAIL when a defect is fixed;
update this file then.

---

## CONFIRMED DEFECTS

### W1 — The reported tier is minted BEFORE adversary penalties (HIGH)
`engine.py:665-681`: `clamp_parent_confidence` returns `(clamped, tier)`,
then `Adversary.apply_verdict` lowers `clamped`, then `result.confidence_tier
= tier` stores the PRE-penalty label next to the POST-penalty score.
Demonstrated end to end: inheritance clamp → CORROBORATED; three MINOR
objections → score 0.60 (PROBABLE); shipped tier stays CORROBORATED. Same
duplicated-logic family as S3: two stages both own the score→tier mapping
and only one is applied. Any downstream consumer of `confidence_tier`
(promotion gates read tiers elsewhere in this repo) sees a band the number
never earned.

### W2 — seal_guard degrades to SEAL when the checkpoints are gone (HIGH)
`checkpoint.seal_guard`: a resumed run whose checkpoint files were deleted
(GC, crash, cleanup script) passes `checkpoints=[]`. Both
`provenance_is_intact([])` branches are vacuously true → `("SEAL", "")`.
The exact sibling pattern R1–R3/Z8 documented ("a verification that degrades
to a pass under its degenerate input"), found once more: an empty evidence
set should be UNVERIFIABLE, which for a resumed run means REFUSE.

### W3 — Resume trusts stored leaf verdicts wholesale (CRITICAL)
The fetch side of resume re-verifies bytes (`replay_ledger` digests); the
answer side does not verify anything. `_leaf_from_payload` and the Evidence
reconstruction in `_run_leaf_checked` restore `confidence_score`,
`source_class`, and `tier` from the checkpoint JSON as-is. End-to-end repro:
edit the persisted `answer_leaf` payload to confidence 0.93 / VERIFIED,
rerun with the same checkpointer → run SEALS with a VERIFIED leaf and no
recomputation, gate, or provenance assignment on that number. `seal_guard`
checks only that fetch bytes are intact — it never binds the SCORED verdict
to anything. A writable checkpoint dir converts directly into sealed tier
inflation.

### W4 — On a resumed leaf, the pipeline's own sandbox counts as an
### independent source (HIGH)
`_answer_leaf`: `n_indep = len({f.source_name ...}) + (1 if sandbox ok else 0)`
fires whenever `trace.independent_keys` is empty — i.e., precisely on the
resume path where the RetrievalTrace is not restored. Repro: delete the
`answer_leaf` checkpoints, rerun a leaf with ONE admitted fetch plus a
model-requested computation → `min_independent_sources=2` is satisfied by
the pipeline's own calculator, the requirement reasons come back EMPTY, and
the parent seals at 0.55/PROBABLE instead of the SPECULATIVE cap. The live
path cannot hit this (trace present), but every resumed quantitative leaf
can. The sandbox is computation, never corroboration.

### W5 — CutoffEnforcer without a signing key admits forged proofs (HIGH)
`cutoff._reject_reason`: the signature check runs only `if self._signing_key`.
With no key configured (the default constructor!), ANY unsigned proof over
ANY bytes with ANY date verifies — the exact "declare any date over any
bytes" forgery path the signature system was built to close, reopened by the
optional key. Fail-open default on the harness's single load-bearing check.
Sibling of Z1 (invalid key downgrades silently). Fix direction: when no key
is configured, admit only proofs explicitly marked legacy-trusted, or refuse
construction without a key.

### W6 — Every pipeline mints its adversary into a throwaway temp dir
### (MEDIUM-HIGH)
`ResearchPipeline.adversary`: `tempfile.mkdtemp(prefix="callisto_adv_")` per
pipeline instance → each run's dissent ledger starts empty and dies with the
process. `precision_of_attack` therefore NEVER accumulates across runs: the
critic's scored track record — the mechanism that distinguishes a real critic
from a rubber stamp, and the planned input to empirical routing — cannot
learn anything beyond one process lifetime. Incentive-shaped: a track record
that always reads n_scored=0 ("insufficient_data") prices silence at exactly
zero, forever.

### W7 — Routing-store writes have no question-level dedupe (MEDIUM)
`write_routing_scores` appends unconditionally; `ModelScoreStore.record` is
append-only by design. Rerunning a batch (the batch runner is explicitly
resumable and re-append-happy) doubles rows for the same question_id:
n inflates, shrinkage toward the chance prior weakens, basis labels upgrade
("sparse" from ONE observation measured twice). Demonstrated: two identical
batch runs → n=2, mean_brier shifts. History being append-only is honest;
feeding duplicates into aggregates is not.

### W8 — ThompsonRoutingPolicy pools all task_classes under a role (HIGH)
`decide(role, candidates)` reads `store.summary(role)` with no task_class
dimension, though every record carries one. Repro: model A has 20 synthesis
losses (brier 0.45), model B has 20 classification wins (0.05); routing a
SYNTHESIS call picks B in 200/200 draws. B has never answered one synthesis
question. This composes with H7.2/H6 of redteam_loop.md: routing scores come
from retro batches written under a single task_class today, so the poison is
latent until a second task_class lands — then cross-class leakage becomes
the default behaviour.

### W9 — Batch resume trusts any non-error checkpoint payload (MEDIUM)
`load_completed` accepts any row with `status != "error"`; `run()` replays it
into results and reporting with no binding between the stored record and the
question's actual outcome. Repro: rewrite the checkpoint payload to
`status="scored", brier=0.0, predicted_probability=1.0` → `build_report`
renders "strongly better than chance". Same trust boundary shape as W3: the
checkpoint store is treated as evidence-grade storage while carrying no
integrity mechanism of its own (unkeyed file, same threat model the HMAC
seal exists for).

---

## HONEST NEGATIVES — attacks that did NOT land (pinned in the suite)

- **harness cutoff via `min(claim_date)`** — initially read as a leak (one
  old question loosening everyone's cutoff). Wrong: strictly-before
  semantics make MIN the STRICTEST date, so mixed-date question sets
  over-exclude, never under-admit. Pinned.
- **magnitude_score sign logic** — edge credited/debited by direction, zero
  edge scores −0.0; no magnitude-masking found.
- **floor_conf** — 5,000 random inputs, never raises. The central quantise
  rule holds everywhere it is actually used; W1 is a sequencing bug around
  it, not inside it.
- **ThreadSafeLedger attribute delegation** — replay's `_w3_replayed_hashes`
  trick survives the facade; dedup across double-replay held.

## PRIORITY ORDER

W3/W9 first (same fix class: bind checkpoint payloads to a keyed digest or
re-verify on load — the seal HMAC already exists), then W2 (empty
checkpoints on a resumed run = REFUSE), then W5 (fail closed on missing
key), then W1 (derive tier from the FINAL score at summary construction),
then W4 (sandbox never counts toward min_independent_sources), then W7+W8
before empirical routing goes live, then W6 (one durable ledger path).

## THE PATTERN, THIRD TIME

Every confirmed break sits at a seam where stage N hands stage N+1 a number
or a record that stage N verified but stage N+1 re-verifies nothing:
clamp→tier (W1), fetch-verify vs answer-trust inside resume (W3), trace vs
fallback counting (W4), optional-key enforcement (W5), per-process ledgers
(W6), append vs aggregate (W7), record vs decision dimensions (W8), stored
row vs report (W9). Component tests cannot see these; only composing the
real pieces and attacking the handoffs did.
