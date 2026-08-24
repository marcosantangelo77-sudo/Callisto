# Source staleness — making health a first-class, persisted property

Branch: `build/source-staleness` (off `backlog2` @ 8123b86). Task 61's
live probes live on `build/source-health` (merged to backlog2); this
task makes their observations DURABLE and feeds them into reasoning.

## What was built

### 1. Persistence — `tools/sources/staleness.py`

A JSON record next to the registry (`tools/sources/source_health_history.json`,
overridable via `HealthStore(path)`; no service, one file):

    last_ok            ISO ts of last probe with non-empty, shape-correct result
    last_shape_match   ISO ts of last passing shape validation
    consecutive_bad    count of recent DEGRADED/BROKEN probes
    last_verdict       OK | DEGRADED | BROKEN | SKIPPED
    last_evidence      evidence string from the latest probe

Derived statuses carry HISTORY — this is the whole point:

| Status   | Meaning |
|----------|---------|
| HEALTHY  | last probe OK |
| STALE    | **worked before, failing now** — silence is a *change*, not a fact; the dangerous case all eleven live-API defects lived in |
| NEVER_OK | probed, never succeeded — no earned claim about silence either way |
| UNSEEN   | never probed |

SKIPPED probes change neither counter (an untested source is not a
failing one). Atomic writes (tmp + os.replace); corrupt store degrades
to empty, never fabricated health. The task-61 CLI
(`python -m tools.sources.health`) persists every observation by
default; `CALLISTO_SOURCE_HEALTH_NO_PERSIST=1` opts out. No network
code here at all — it consumes already-collected `ProbeResult`s, so
the no-socket test guard is untouched.

### 2. The null classifier now has evidence — `tools/gaps.py`

`classify_null_kind` (THE single membership rule) previously had a
blind spot exactly where the eleven defects hid: a source returns
200-with-zero-results, nothing errors, nothing is rejected-with-reasons,
so the trace read as HONEST NULL ("the literature is silent").

Now: when an otherwise-honest null leaned on sources whose health
history says STALE (previously returned data for known-good queries,
currently empty/broken), the verdict amends to RETRIEVAL_FAILURE with
the evidence named in the explanation. Asymmetric by design:

- Sources with NO history or NEVER_OK records **do not flip** anything —
  absence of evidence about a source must not invent a failure.
- If the staleness module or store is unavailable, the classifier
  degrades to its old behavior rather than blocking.
- `amend_null_classification()` is the single place the rule lives;
  both `classify_gap`'s path and `classify_null_kind` can reach it.

This is the distinction the OwnerAction taxonomy was designed for:
RETRIEVAL_FAILURE carries an owner action (RETRY / investigate source);
HONEST_NULL carries ACCEPT_UNKNOWABLE. Feeding real health data in
makes that mapping correct instead of guessed.

### 3. Degraded sources are visible at conclusion time — `tools/pipeline/engine.py`

`PipelineResult.source_coverage` = {stale, never_ok, unseen} among the
run's PLANNED sources (`session.sources`) at conclusion time. When any
planned source is degraded:

- a note lands in `result.notes`, AND
- the sealed conclusion TEXT itself carries a SOURCE COVERAGE WARNING
  (both `session.summary.conclusion` and `result.conclusion`) — a user
  reading only the output sees that the conclusion was drawn from an
  incomplete registry without opening a debug field.

An absent/unreadable history degrades to silence — coverage claimed by
nobody — rather than asserting a clean bill of health.

## Constraints honored

- Classification and disclosure ONLY: no confidence score is raised or
  lowered anywhere in these paths (verified end-to-end: sealed run with
  a stale planned source kept confidence 0.55 unchanged).
- No silent infinite retry, no auto-disable.
- Network remains opt-in behind `CALLISTO_SOURCE_HEALTH_NET=1`; this
  task added zero network code.
- No-socket guard intact; all new tests are offline (fake probes +
  temp-dir stores).

## Verification

- 13 new offline tests (`tests/test_source_staleness.py`): store
  semantics (OK→DEGRADED = STALE with `last_ok` preserved;
  BROKEN streaks; SKIPPED neutrality; persistence roundtrip; corrupt
  store), amendment rule (flip / no-history-no-flip / NEVER_OK-no-flip /
  non-honest-null untouched), gaps integration, CLI persistence.
- Full targeted subsets green: gaps + verdict wiring + pipeline + W3
  checkpoint + estimate wiring + I3 synthesis = 165 passed.
- End-to-end with the REAL pipeline + fixture transport:
  - sealed run, openalex STALE in history → warning present in the
    sealed conclusion text and session summary; confidence untouched.
  - empty response from a stale source → `gap_kind = retrieval_failure`
    with "recent history of good results" in the explanation.

## Honest limits

- History quality depends on someone actually running the opt-in probe;
  nothing here schedules it (deliberately — scheduling is an owner
  decision). An UNSEEN registry produces no warnings, which is honest
  but means deployment should wire the probe into whatever periodic
  maintenance exists.
- `source_coverage` compares against ALL planned sources
  (`session.sources`), not per-leaf selection. A leaf-specific view is
  possible via `gaps.source_names_from_trace()` if needed later.
