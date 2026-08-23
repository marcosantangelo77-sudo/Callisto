# FINDINGS — Mutation testing (VERIFY pass)

**Question:** do the 1,922 passing tests actually CATCH anything, or do they
just execute code?

**Method:** 45 hand-curated, realistic defects (flipped comparisons, inverted
boundaries, dropped clamps, negated penalties, skipped verifications, widened
ceilings) injected one at a time into COPIES of the ten modules where being
wrong is expensive. Each mutant ran against the module's own test suites plus
the cross-cutting seal/lifecycle suites. Harness: `scripts/mutation/`;
per-mutant raw results: `mutation_results.json` (this directory).

Production source was never modified.

## The number

| Module | Mutants | Caught before this pass | Score now |
|---|---|---|---|
| agp/thresholds.py | 4 | **0** | 4/4 |
| tools/hypothesis.py (the gate) | 4 | 2 | 4/4 |
| tools/pipeline/synthesis.py | 2 | 1 | 2/2 |
| tools/research_program.py (inheritance rule) | 6 | 4 | 6/6 |
| agp/ensemble.py | 5 | 3 | 5/5 |
| tools/edge.py (money) | 6 | 5 | 6/6 |
| agp/provenance.py | 5 | 5 | 5/5 |
| agp/adversary.py | 5 | 5 | 5/5 |
| tools/kelly.py (money) | 4 | 4 | 4/4 |
| tools/pipeline/retrieval.py | 4 | 4 | 4/4 |
| **TOTAL** | **45** | **29 (64%)** | **45/45 (100%)** |

**The suite caught 64% of injected defects. Twelve defects — more than one in
four — passed straight through 1,922 green tests.**

## The finding: high-traffic constants with zero value assertions

`agp/thresholds.py` is imported by at least 18 test files and by the
orchestrator, the engine, memory, claims and kelly. Its score was **0/4**.
Every single constant could drift:

- `TIER_VERIFIED_MIN` 0.90 → 0.85: unearned confidence reclassified as
  VERIFIED. Nothing noticed.
- `SECONDARY` ceiling 0.75 → 0.80: web-search confidence promoted into a band
  it has not earned. Nothing noticed.
- `CRITICAL` contradiction penalty 0.15 → 0.00: a critically contradicted
  session seals at full confidence — the exact "system flatters itself"
  failure BUILD_MANDATE §4 names as the cardinal defect. Nothing noticed.
- `DB_CONFIDENCE_FLOOR` 0.30 → 0.10: sub-SPECULATIVE sessions persist as if
  scored. Nothing noticed.

This is the precise pathology the brief predicted: tests *import* the module
(coverage ~100%) without ever *asserting* its values (mutation score 0%).
Coverage measures execution; mutation measures checking.

The same shape appeared elsewhere:

- **tools/hypothesis.py (gate)**: widening `n < 8` to `n < 80` in
  `get_adaptive_p_value_threshold` survived — the small-sample relaxation of
  the promotion gate would have applied up to 10× longer than designed. And
  `binomial_pvalue(0 wins)` returning 0.0 instead of 1.0 survived — zero
  successes reported as maximally significant, i.e. the gate could pass on no
  evidence in one direction of the fix.
- **tools/research_program.py**: lowering `MIN_RESOLVED_FOR_LIFT` 5 → 2 and
  negating the staleness penalty both survived. The inheritance rule — the
  capstone — would have let a parent claim lift off SPECULATIVE on two
  descendants, and stale (unresolved-by-deadline) descendants would have
  RAISED rather than lowered the ceiling.
- **agp/ensemble.py**: zeroing `UNANIMITY_BONUS_PENALTY` survived, and so did
  `all()` → `any()` in unanimity detection — one grumpy critic among three
  silent ones reading as consensus.
- **tools/edge.py**: `edge >= min_edge AND ev > 0` surviving as an OR means a
  negative-EV bet inside the vig gap could be flagged actionable.

## What was added

`tests/test_verify_mutation_kills.py` — 24 tests, every one written to kill a
specific surviving mutant, all verified by re-running the harness (45/45
killed after). Includes boundary-exactness pins (tier boundaries inclusive,
adaptive-threshold schedule exact per n), consensus semantics (`all`, not
`any`), the AND between edge-gate and EV-gate, and a 2,000-sample property
probe that no clamp path can raise a score.

## Two of the 20 pre-existing failures are themselves live production defects

20 tests were failing before this pass began (verified at base commit
d5e2167; none are mine). Most are environmental (xgboost/libomp, xlsx
workbook emission). Two are not — they are correct tests catching real,
still-unfixed production defects:

1. **The claim journal is NOT tamper-evident** (agp/claims.py ClaimStore).
   `test_journal_is_hash_chained_and_rejects_retroactive_edits` fails with
   "DID NOT RAISE". Reproduced directly: rewriting journal line 1 to set
   `confidence=0.95, status=confirmed` and load() returns the forged state
   silently. Root cause: each entry stores its own `prev` pointer inside the
   same blob that is hashed — an attacker rewrites content AND the prev
   reference together, and every per-line digest check passes. The chain has
   no external anchor (e.g. the store's directory index should pin the
   genesis-entry digest, or entries should chain over the PREVIOUS line's
   hash verified against a copy kept outside the rewritten file). The module
   docstring promises "rewriting history to flatter yourself breaks the
   chain"; it does not.

2. **Preregistration amendments hijack default scoring**
   (agp/preregistration.py). After one amend(), plain `score(...)` — no
   criteria argument, i.e. the DEFAULT path used by Claim.resolve — scores
   against the AMENDMENT and reports `used_amendment=True`. The contract
   (and the failing test) says scoring without explicit criteria must use
   the sealed ORIGINALS; `effective_criteria` returns the latest amendment
   instead. A mid-study criteria change silently retroactively redefines
   what "confirmed" means for every subsequent resolution — precisely the
   evidence-rewrite mechanism the night's diagnosis condemned.

Both need production fixes by whoever owns agp/claims.py and
agp/preregistration.py; editing them was out of scope for this pass.

## Bonus finding (not mutated — found while writing the probe)

`clamp_confidence_provenance` (agp/provenance.py) and
`clamp_parent_confidence` (tools/research_program.py) both finish with
`round(x, 2)`, which can RAISE a score by up to 0.005 — the same R3 rounding
bug class (round(0.836, 2) == 0.84) already fixed in agp.adversary with
floor(). Magnitude trivial; direction wrong; the invariant these modules
advertise is "only ever pulls DOWN". Production source was read-only for this
pass, so the probe pins the actual contract and documents the leak. One-line
fix each: `math.floor(x * 100) / 100`.

## Caveats

- 45 curated mutants is a sample, not exhaustive coverage; the catalog lives
  in `scripts/mutation/mutation_catalog.py` and should grow alongside the
  modules.
- Each module ran against its targeted suites (~5 min total), not all 2,072
  collected tests; a wider net could only kill MORE mutants, never fewer.
- Two ML test files fail to collect for a pre-existing environmental reason
  (xgboost missing libomp on this Mac); unrelated to any target module.
- `hypothesis` (property-based testing) was missing from the environment;
  three existing suites needed it to collect at all. Installed.

## Verdict

Before this pass, roughly one defect in three injected into the money paths
and the epistemics core sailed through the entire suite. After: every
injected defect is caught, and the killing tests now guard the boundaries
themselves, not just the happy paths. Rerunning the harness is one command:
`python3 scripts/mutation/run_mutations.py`.
