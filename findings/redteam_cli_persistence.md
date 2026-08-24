# RED TEAM — CLI front door, run-record persistence, migrations (2026-08-24)

Worktree: `loop/` on branch `redteam/cli-persistence`.
Repro tests: `tests/test_redteam_cli_persistence.py` (6 failing-before on
pre-fix code, committed first at 53dfb95 so failures are verifiable).
All migration work ran against THROWAWAY databases in /tmp — the workstation
DB at memory/callisto.db was never touched. No confidence score was raised.

---

## D1 — Run records carry no proof; tampering displays as SEALED
**Families 1 (inert verification) + 9 (looks exactly like success).**

The claim "the run record carries its own proof" is false. The record is
plain JSON with no integrity field of any kind (no seal hash, no content
hash — see `test_d1_record_lacks_any_integrity_field`). The AGP seal that
the engine computed over the *session payload* (`AGPSession.seal()`,
engine.py:981) is **thrown away by the CLI**: `_result_record` never copies
`result.session.seal_hash`, and `verify_seal` has zero callers anywhere in
the CLI/persistence path.

Attack performed: took a real persisted record, rewrote conclusion to
"TAMPERED — buy now" and confidence to 0.95/ESTABLISHED, and `callisto show`
displayed it as `SEALED : ESTABLISHED 0.95` verbatim.

**Fix direction:** persist `session.seal_hash` + the canonical session dict;
`show` must run `AGPSession.verify_seal` and print UNVERIFIED/tampered when
it fails or the field is absent (absence fails closed — family 3).

## D2 — Same-second asks can silently destroy each other's records
**Family 3 (loss treated as success) + family 6-adjacent (collision
direction always loses data).**

`_persist_run` builds the filename as `timestamp + abs(hash(question)) % 10000`
and `os.replace`s without checking existence. Two different questions whose
hashes collide mod 10000 asked within the same second → the second record
overwrites the first, no error, exit code still reflects "sealed". Found a
real colliding pair (`q4`/`q294` under one PYTHONHASHSEED) and reproduced
the silent destruction (`test_d2_same_second_same_bucket_overwrites`). With
concurrent asks (dispatcher runs parallel instances) this is not exotic.
Also: no fsync before `os.replace` — a power-loss can leave an empty/
partial file after the rename.

**Fix direction:** uuid4 suffix per record; refuse (never replace) if the
path exists; fsync tmp file before rename.

## D3 — `show` verifies artifacts but NOT fetch provenance; ok-marks launder edits
**Family 3 (absence treated as success; direct echo of C1).**

`_cmd_show` re-hashes artifact bytes but prints each fetch's
`content_sha256` **without ever checking it against anything** — including
the empty string. Reproduced: a record whose only fetch had
`content_sha256: ""` displayed as ordinary provenance with no warning
(`test_d3_...`). Combined with D1, `show` printed a tampered conclusion
adjacent to a `[ok]` artifact line — the one real check in the display
lends credibility to everything around it.

**Fix direction:** flag empty/non-hex fetch digests as unverified; where the
raw fetched bytes exist in a cache keyed by that digest, re-hash them.

## D4 — doctor reports OK with a dead default tier and a corrupt database
**Family 1 (a check that cannot fail) — the exact shape named in the brief.**

Live on this box right now: gpu1 (the DEFAULT tier, localhost:8080) refuses
connections — `callisto ask` correctly exits 2 "provider 'gpu1' unhealthy" —
while `callisto doctor` exits 0 printing `doctor: OK`. Doctor's provider
section only checks that providers.yaml *parses*; it performs zero
reachability probes even though `ProviderRouter.check_health` exists and is
one call away. Its "database" section is `print(present: Path(db).exists())`
— a corrupt/garbage file at CALLISTO_DB_PATH reports `present: True`,
doctor OK (reproduced: 3.5 KB of junk bytes → exit 0, `test_d4_...`).
The source-registry check only proves adapters are *registered*, not that
any source works.

So `ask`'s health gate is real but `doctor`'s is theatre: the tool that
exists to answer "can this box run a live question right now?" answers OK
when the honest answer is no.

**Fix direction:** call `router.health_report()` (all tiers, ~5s), fail on
default tier unreachable; for the DB, open it and run `PRAGMA quick_check`.

## D5 — Migration bootstrap wedges pre-framework databases permanently
**Family 8-adjacent (a maintenance routine creating a state nothing can
recover from) / new family: trust-based bookkeeping.**

On a THROWAWAY db mimicking a pre-framework checkout (has `hypotheses`,
no `schema_migrations`): `bootstrap_existing_db` marks versions 001–012
applied **purely because the hypotheses table exists** — it never verifies
those migrations are actually satisfied — then 013 immediately fails
(`no such column: edge_threshold`). Result: recorded={1..12}, 13 unapplied,
and EVERY subsequent startup raises the same error forever. The DB is
permanently wedged; there is no command to unbootstrap. Reproduced twice,
second run identical (`test_d5_bootstrap_wedges_pre_framework_db`).
This branch's own schema doesn't hit it because fresh DBs bootstrap
nothing — but any DB created before the seam work (e.g. restored from an
old backup) does, and the failure looks like a bug in 013, not in the
heuristic that trusted it.

**Fix direction:** verify satisfaction column-by-column before marking a
version bootstrapped; on mismatch, apply rather than mark; provide an
escape hatch (`--rebuild-bookkeeping`) instead of a permanent wedge.

## Migrations that held up (tested, throwaway DBs only)
- Double-apply: runner correctly skips applied versions; second full run
  applies nothing (15 skipped).
- Failure atomicity: sabotaging 013 to CREATE TABLE then raise → rollback
  worked, marker table absent, version 13 unrecorded, later migrations not
  attempted, recovery clean. Pinned as `test_d6_mid_migration_failure_rolls_back_ddl`.
- Kill -9 between migrations (simulated via commit-hook exit): DB left at
  exact all-of-N boundary; resume applies 13-15 cleanly. Good.
- Rollback (`down()`): 016 from improve/rotating-0823-175824 drops both
  tables cleanly. Note: 016 is NOT on this branch's landmerge — its module-
  level `MIGRATION_VERSION = 20260824` constant is ignored by the runner
  (which keys off the filename regex); harmless today, a trap if someone
  renumbers a file without renaming.
- One real nit in 016: `CREATE TABLE IF NOT EXISTS predictions` silently
  keeps a WRONG pre-existing table shape until the index creation raises
  mid-transaction — caught by rollback, but the error message points at
  `claim_id`, not the collision.

## D7 (minor) — `show '*'` crashes with a raw traceback
`_load_run` globs `f"{run_id}*.json"` unsanitised. `show '*'` raises
`ValueError: Invalid pattern: '**' can only be an entire path component`
straight to the user (pinned by `test_show_glob_metachar...`).

## Probed and NOT guilty
- Artifact-store concurrency: unlike the index that lost 28/75 entries, both
  thread-level AND cross-process (flock layer, 4 procs × 15 puts, warm root,
  120 objects) put paths lost ZERO entries. The A14 lock work holds.
- Torn `.tmp` writes: crash mid-write leaves `*.json.tmp`, which neither
  `runs` nor `show` picks up — torn files fail closed. Good.
- Control characters: NUL/ESC in questions persist fine (JSON-safe) and
  `show` passes them through raw — terminal escape injection is possible
  from a question containing `\x1b[...`, worth sanitising, low severity
  since the question author is the operator.
- crossrun JSONL store: append-only, thread-locked, torn-line skip — sound.

## Summary table

| # | Defect | Family | Status |
|---|--------|--------|--------|
| D1 | records unsealed; show displays tampered content as SEALED | 1+9 | confirmed, repro failing |
| D2 | same-second hash-bucket collision silently destroys records | 3 | confirmed, repro failing |
| D3 | fetch content_sha256 never checked; absence passes | 3 | confirmed, repro failing |
| D4 | doctor cannot fail: dead tier + garbage db → OK | 1 | confirmed, repro failing |
| D5 | bootstrap trusts 1-12 then wedges pre-framework DBs | 8-adj | confirmed, repro failing |
| D7 | `show '*'` raw traceback | minor | confirmed, repro failing |

No fixes were landed from this pass (red-team role): repro tests are
committed failing-first on `redteam/cli-persistence` so the fixing agent
can flip them green one by one.
