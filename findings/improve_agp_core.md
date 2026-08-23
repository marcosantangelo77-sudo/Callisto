# AGP PROTOCOL CORE — improvement pass (build/cli-front-door)

**Area chosen: the AGP protocol core** (agp/ — adversary.py, claims.py,
ensemble.py, human_critic.py, prereistration.py, provenance.py,
research_program.py, thresholds.py, __init__.py).

Why this one: CLI was covered twice (improve_cli, improve_cli_run_persistence),
the calibration/retrodiction harness and edge quantification in the two runs
before that, retrieval/synthesis/checkpointing/routing/schema before those.
The protocol core is the part BUILD_MANDATE calls "the thing nobody else has"
— the earned-confidence machinery every other component is judged by — and no
improve pass had owned it. It is also small enough (7 modules, ~2,600 lines)
to actually read end to end, which is what this pass did.

## What was wrong — measured

**The critic track record does not survive a process restart.**
`AdversaryLedger` records lifecycle updates by APPENDING an updated copy of
the objection to its JSONL (raise -> overruled -> resolved = 3 lines). But
every read path replayed the journal as one-objection-per-line:

- `calibration()` counted `len({id(o) for obs ...})` over ALL loaded copies
  for n_raised (inflated 3x by the lifecycle length), and `all_resolved()`
  kept only objections whose FIRST copy already had an outcome — on a fresh
  load, first copies never do.
- Reproduced (fresh process over a raise->overrule->resolve journal):
  `n_raised 3 -> expected 1`, `n_scored 0`, `precision_of_attack null`,
  `verdict insufficient_data`. In-memory it read correctly (`n_raised 1`,
  `n_scored 1`, `too_harsh`). The ledger's own docstring says this number
  "is what distinguishes a real critic from a rubber stamp" — after any
  restart it was blank, and `calibration_by_model()` (which feeds W4's
  empirical routing) was blank with it.
- The correct last-wins replay existed ONLY inside agp/human_critic._latest,
  with a comment saying so ("we cannot edit that module") — a duplicate read
  model kept alive by an artificial ownership boundary.

**Also checked and found sound** (so nobody re-audits): seal keying and
rotation (HMAC + legacy fallback, constant-time compares); preregistration
immutability (__setattr__ guard, sealed amendments chain); the asymmetry
invariant across apply_verdict / PanelVerdict.apply / clamp_with_ensemble /
clamp_with_human_agreement (floor-rounded, no raise paths — I traced each);
ClaimStore hash-chain verification; the inheritance rule's Wilson-bound
construction; ensemble distinctness normalization resolving conservative.

## What changed (1 commit)

fef694f:

- `AdversaryLedger._latest()` — last-wins journal replay keyed on
  (claim_id, created_at, text) — is now the single read model behind
  `calibration()`, `all_resolved()` and `calibration_by_model()`. The file
  format is unchanged; still append-only; full history stays auditable by eye.
- `calibration()` counts DISTINCT objections instead of journal lines.
- `agp.human_critic.HumanCritic._latest` delegates to the shared method;
  ~30 lines of divergent duplicate parsing deleted.
- 5 regression tests (tests/test_build_r7_adversary_ledger_reload.py),
  including reload==in-memory byte-equality and journal-stays-append-only.

## Before/after numbers

| measure | before | after |
|---|---|---|
| reloaded calibration() | n_raised=3, n_scored=0, verdict=insufficient_data | n_raised=1, n_scored=1, verdict=too_harsh |
| reloaded vs in-memory | disagree (blank vs scored) | identical |
| calibration_by_model after restart | empty per-model records | real per-critic scores |
| duplicate journal-replay implementations | 2 (ledger + human_critic) | 1 |
| area tests | existing suites | +5 |

Full suite: 26 failed / 2,130 passed both WITH and WITHOUT my edits
(verified by stashing my three files and re-running: identical failure set —
backtest_e2e x11, openpyxl-missing workbook tests, prop_scanner, plus other
instances' uncommitted work-in-progress files). Sports failures pre-exist on
this Mac; nothing in agp/ touched them.

## Honest caveats

- A concurrent actor was actively modifying the working tree during this
  pass (engine.py, retrieval.py, sources/*). My diff touches ONLY
  agp/adversary.py, agp/human_critic.py and the new test file; a snapshot of
  the tree as I found it is preserved at branch recover/agp-pass-wip.
- The ledger's objection identity key (claim_id, created_at, text) means two
  genuinely identical objections raised in the same microsecond by the same
  path would collapse into one. Not observed in practice; noted for honesty.
- Deeper AGP-core work I considered and rejected: persisting ProvenanceLedger
  beyond checkpoints (a real durability gap, but it belongs to the pipeline/
  storage seam, not the protocol core, and would need a design decision about
  where fetch records live). Left for a run that owns that seam.
