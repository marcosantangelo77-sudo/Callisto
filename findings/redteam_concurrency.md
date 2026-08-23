# RED TEAM FINDINGS — concurrency and races

**Claim under attack:** "an honest concurrent workload cannot corrupt a
claim's calibration record or launder/duplicate pipeline evidence" — i.e.
the tamper-evidence machinery distinguishes attack from accident, and the
checkpoint layer's idempotence contract ("re-running cannot duplicate
evidence or ledger entries", `tools/pipeline/checkpoint.py` docstring)
holds when two actors race.

**Why this surface:** no prior redteam pass covers it (confidence F1–F7,
loop V/H, provenance R1–R8, seal Z1–Z10, money, retrieval all landed;
`git log --all --diff-filter=A` shows no concurrency file). And the
surface is maximally exposed: `ClaimStore` and `FileCheckpointer` are
shared mutable files with **zero** synchronization — no lock, no version
counter, no compare-and-swap anywhere in either module. Everything else
in the repo that touches shared state (`db_writer.py`) was built around
a single-writer coordinator; these two predate that discipline.

Tests: `tests/test_redteam_race_claims.py`,
`tests/test_redteam_race_checkpoint.py`. All failures below are
reproducible with the public API only — no fabricated state, no
simulated corruption.

## CONFIRMED BREAKS

### RACE-C1 — Concurrent saves of one claim fork the hash chain and read as TAMPERING (CRITICAL)

`ClaimStore.save()` reads the last journal line's hash, then appends.
Nothing serializes the read-append pair. 8 threads saving one claim
(barrier-synchronized, each attaching its own evidence) produce a forked
chain: `load(verify=True)` raises *"tampering detected … history is not
trustworthy"* for purely honest writes.

This is worse than data loss. The chain exists so an operator can trust
the alarm; here the alarm fires on the system's own normal operation,
which trains everyone to dismiss it — the same alarm class as Z5 (chain
binds lines, not content) but reachable **without any attacker at all**.
Generalized by property test C5 over random thread counts (2–12) and
scheduling jitter: forks reproduce across interleavings, not just one
lucky schedule.

### RACE-C2 — Lost update silently erases evidence from the calibration record (CRITICAL)

Two holders of the same claim id attach different evidence and save.
`save()` has no version counter, merge, or CAS: holder A's save
overwrites B's evidence set wholesale while still appending to the
journal — the record of the loss looks exactly like a legitimate
transition. Test demonstrates `"ev 2"` vanishing after both saves
succeeded. Evidence may be superseded by a writer who SAW it; here it is
erased by one that never did. This directly corrupts the thing the whole
epistemics layer exists to protect: what we believed and on what basis.

### RACE-C7 — Empty digest dedup drops distinct evidence at the resume boundary (HIGH)

`replay_ledger()` dedups on `rec["content_sha256"]`. A fetch record with
no digest contributes the literal string `""` to the seen-set, so every
later digest-less fetch — whatever its body — is counted
`skipped_duplicates` and never reaches the ledger. Demonstrated: bodies
`alpha` and `beta`, one ledger observation. Resumed runs silently lose
evidence, lowering confidence honestly earned — or worse, making the
run's evidence set diverge from what synthesis actually consumed.
(Sibling of R1 "missing digest = zero integrity check": same root cause
— the empty string is treated as a value — different symptom. Two call
sites, one fix: reject/derive digests, never key on `""`.)

### RACE-C4 — Checkpointer cache-miss thundering herd duplicates stage work (MEDIUM, contract violation)

`run_stage()` is load-check → execute → save with no lock. 10 concurrent
coroutines resuming the same fresh stage executed the underlying work
10 times (test asserts ==1). For fetch stages this duplicates exactly
what the module docstring promises impossible ("no duplicate fetches,
ledger entries, or artifacts"), burns source rate budget (see MORNING
REPORT: SEC/ClinicalTrials already 403 this machine), and two racers can
persist *different* bytes under the same input_hash — last writer wins,
and the resumed consumer may reason against payload A while disk holds
payload B.

## HONEST NEGATIVES / NOT PUSHED FURTHER

- **Torn appends (C1b): could not break.** Six concurrent large-blob
  appends to one JSONL file produced zero unparseable lines on macOS
  (APFS append atomicity at these sizes). I believe larger payloads or
  NFS would tear it, but I could not demonstrate it here — reported as
  unproven, not safe.
- **GC deleting young checkpoints (C6): held.** gc() snapshots via
  list_all(), but every unlink targets an age-verified old checkpoint
  and open-claim guards held in all interleavings tried. The snapshot-
  then-unlink window can delete a re-saved copy of an OLD key, but keys
  are content-addressed, so a re-save of the same key is byte-identical.
- Did not attempt multi-process (fork) races or SQLite-backed stores;
  single-writer coordinator in db_writer.py looked genuinely sound on
  inspection.

## WHAT TO FIX (leverage order)

1. Per-claim lock (or flock on the journal path) inside ClaimStore.save/
   load — closes C1 and makes C2 detectable; add a monotonic version
   field checked at append (CAS) to make C2 fail loudly instead of
   silently.
2. replay_ledger + integrity checks: treat missing digest as failure OR
   dedup by `_sha(body)` — never key on `""` (fixes R1's sibling C7).
3. Per-(run, stage) async lock in run_stage (or accept-and-coalesce:
   first runner executes, others await its save).

## THE PATTERN, THIRD TIME

R-report said it, Z-report repeated it: the same rule living in two
places, one fixed. Here it is the empty-string digest again (integrity
check site fixed in spirit; dedup key site not), and more broadly:
tamper-evidence designed against an adversary but never tested against
the system's own honest concurrency. Every guarantee asserted in a
docstring ("idempotent", "cannot duplicate") is single-threaded until
proven otherwise.
