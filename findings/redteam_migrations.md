# Red Team: Migrations & Schema Evolution

**Date:** 2026-08-24 · **Branch:** `redteam/rotating-0824-223125`
**Surface:** `tools/migrations/**` (15 migrations + runner), `tools/schema/engine.py`,
`tools/schema/core.py`, `plugins/sports/schema.py` — and the startup sequence that
wires them (`api.py:843-868`).
**Tests:** `tests/test_redteam_migrations_evolution.py` — 11 tests, **6 failing
expectations (the findings), 5 passing controls/characterizations.**
Nothing outside my two owned files was edited.

---

## Surface choice, and why

Every other named surface has a red-team pass; this one had none, despite being
the only subsystem whose entire job is to change state under a running system.
Three facts made it the highest-priority unattacked ground:

1. **MORNING_REPORT says migrations 013 and 015 "have never been run" against
   the real database.** Untested code that rewrites production data on next boot.
2. `api.py` runs `ensure_schema()` **before** `apply_pending_migrations()`
   unconditionally at every startup — two evolution systems racing on one table,
   with a documented ordering contract nobody had verified.
3. The b5 seam tests exercise migration 013 against a hand-built fixture and
   insert post-seam rows manually. No test anywhere runs the *real* startup
   sequence and then speaks to the result through the *real* writer.

## Method choice, and why

**Differential between the schema histories this codebase can produce**
(fresh-at-head vs evolved-through-migrations vs hand-migrated legacy), plus a
property sweep over duplicated rule tables, plus family-1 hunts ("what reads
this evidence?"). Rotation justification: last two passes were both mutation
runs; adversarial input and seam analysis are also spent. Differential against
a *second oracle* — the application's own writes — is what mutation cannot do
here, because the suite's 11,509 tests never execute the real sequence.

Families hunted: **#2** (fix lands in one copy), **#3** (absence treated as
success), **#4** (structural text standing in for the thing itself), **#1**
(verification layer nobody calls). All four hit.

---

## RT-MIG-1 — CRITICAL · the startup sequence breaks hypothesis creation on every database

**Family #2/#7. Reproduced twice:** raw SQL (`test_rt_mig1_production_writer_survives_full_startup_sequence`)
and through the public API (`test_rt_mig1_real_hypothesis_manager_create_after_startup`).

Run exactly what `api.py:lifespan` runs — `ensure_schema()` then
`apply_pending_migrations()` — on a fresh DB, then execute the production
writer's INSERT from `tools/hypothesis.py:580`:

```
OperationalError: table hypotheses has no column named sport
```

The chain:

1. `plugins/sports/schema.py:194` still creates `hypotheses` **welded**
   (`sport NOT NULL`, `market_type NOT NULL`) — there is no seam-shaped DDL
   anywhere in `ensure_schema`'s path.
2. Migration 013 therefore fires on **every** database, fresh included
   (`_needs_migration` is True whenever `sport` exists), rebuilding
   `hypotheses` without those columns.
3. The production writer still inserts them.
4. The compat layer that was supposed to bridge this —
   `tools/schema/compat.py`, cited verbatim at `plugins/sports/schema.py:1241`
   ("compat views … keep ``SELECT sport FROM hypotheses`` working during the
   transition") — **does not exist in the tree.**

So the system is one API restart away from dead hypothesis creation, on this
branch, today. **Known-but-stranded:** commit `e4edcca` on unmerged
`review/rotating-0823-155500` measured and fixed exactly this ("Measured on a
fresh DB… create_hypothesis died"). It never landed here. That is the review
doc's D1 pattern repeating: the diagnosis exists, the fix sits on an island,
and master still ships the break. My tests pin it independently so the merge
cannot silently regress it.

Why 11,509 tests miss it: none run the real sequence end-to-end (family #7 —
tests pass for the wrong reason; they test the fixture, not the pipeline).

## RT-MIG-2 — HIGH · migration 013's FK repair silently drops every index on the child tables

**Family #4 (stored CREATE text standing in for the table's identity).**
`013._repair_child_fks` rebuilds each damaged child (`backtest_events`,
`backtest_runs`, `paper_trades`, `hypothesis_stats`, masters_*) from its
`sqlite_master.sql`. A table's stored SQL contains **no indexes or triggers**,
so after the repair:

- `idx_bt_events_event_id` (migration 003 — shipped specifically because
  `/system/full-status` full-scanned 112k rows) — gone.
- `idx_bt_events_run`, `idx_bt_events_signal`, `idx_bt_events_local_date` — gone.
- Same for every other repaired child.

Rows survive; performance fixes vanish. Nothing logs, nothing recreates.
Test: `test_rt_mig2_fk_repair_preserves_child_indexes` (fails today; control
assertion proves rows are preserved, isolating the defect).

## RT-MIG-3 — MEDIUM · migration 015.down() writes NULL confidence into live rows

**Family #3 (absence treated as success).** `down()` restores confidences via

```sql
UPDATE hermes_learnings SET confidence =
    (SELECT b.confidence FROM hermes_learnings_conf_backup_015 b WHERE b.key = ...)
```

For any row created **after** the backup snapshot, the subquery yields NULL —
and `hermes_learnings.confidence` is nullable, so a rollback silently corrupts
every learning recorded since the migration ran. Downstream readers do
`float(conf)` / treat falsy as missing. Test:
`test_rt_mig3_down_never_nulls_rows_created_after_migration` (fails;
companion control `..._restores_preexisting_row_confidence` passes, proving
the restore mechanism works for backed-up keys — the defect is precisely the
unbacked case).

## RT-MIG-4 — CONFIRMED DRIFT · duplicated team→timezone tables already disagree

**Family #2, caught red-handed.** Migration 010 inlines copies of
`tools/game_dates`' four sport TZ maps ("intentional duplication … must match
exactly", per its own comment). Nothing pinned them; the property sweep found
they already drifted: canonical `NHL_TEAM_TZ` carries **both**
`Montreal Canadiens` and `Montréal Canadiens`; the migration copy has only the
former. Whichever copy backfills a row first wins forever (writes are guarded
by `local_game_date IS NULL`), so the copies cannot heal each other. Test
parametrized over all four sports; NHL fails today, the other three hold.

Also noted: `010_local_game_dates.py`'s docstring and logger say "**Migration
007**". Cosmetic until someone audits `schema_migrations` against file contents.

## RT-MIG-5 — MEDIUM · `schema_migrations.checksum` is stored but read by nobody

**Family #1 (verification layer whose input nobody consumes).** `runner.py`
stores sha256 of each applied migration's source "so a later audit can detect
someone edited 002 after it was applied." Grep finds zero consumers. A tampered
checksum produces no error, no warning, and no field in the returned status —
detection currently requires hand-writing SQL against the bookkeeping table,
which is not detection. Test:
`test_rt_mig5_tampered_applied_checksum_is_detectable_via_public_api` (expects
`checksum_mismatches` in the status dict or a raise; fails today). Either
verify on every run or delete the column; dead evidence is worse than none
because it *looks* like protection.

## Characterization — the bootstrap guarantee in `__init__.py` is false

`tools/migrations/__init__.py` promises: *"for existing DBs the bootstrap step
marks every migration as already-applied so nothing re-runs."* False for every
DB this code creates: `ensure_schema()` seeds `schema_migrations` with
20260421/20260422 **first**, the table is never empty when the runner looks,
so `bootstrap_existing_db` returns 0 and migrations 001–012 all execute against
existing databases — including **004, which DELETEs orphan rows**, on the real
workstation DB at next boot. Probably desirable hygiene; absolutely not what
the operator was told would happen. Pinned by passing test
`test_char_ensure_schema_touches_bootstrap_decision`.

---

## Serious attempts that did not produce defects (reported honestly)

- **015 decay idempotency claim.** The docstring claims clamp+decay "are
  functions of current state" and safe to re-execute. False in isolation —
  `_decay` multiplies the *current* value, so direct re-runs compound
  (conf×f²). But within the framework it is unreachable: the version row
  prevents re-running, and `down()` restores originals from backup before any
  second `up()`. Doc overclaim, not a live defect.
- **013 step-1 ext-copy guard.** I tried to lose domain data through
  `INSERT OR IGNORE` into `hypothesis_sports_ext` (NOT NULL sport dropping
  rows silently). The subsequent `ext_copied < pre_count` abort catches every
  route I could construct. Fail-closed; no defect found.
- **ensure_schema's log-and-continue on failed DDL.** Deliberate design with
  loud logging and per-boot retry; the residual risk (typo'd table name =
  permanently incomplete schema that boots "successfully") is real but is a
  policy question, not a defect to pin here.
- **014's joint-rollback claim.** Its failure message says "Migration 013 will
  roll back with it (same transaction)" — the runner commits 013 before 014
  begins, so this is false. I could not construct an honest DB where 014
  hard-fails *after* a successful 013 without manufacturing corruption inside
  the fixture, so this stands as a prose observation, not a test.

## Recommended order of operations

1. Land `e4edcca` (or equivalent dual-write) from
   `review/rotating-0823-155500` — RT-MIG-1 is the only one of these that
   takes down a core lifecycle path on next restart.
2. Recreate indexes after `_repair_child_fks` rebuilds (RT-MIG-2).
3. Make `down()` skip absent keys instead of writing NULL (RT-MIG-3).
4. Verify checksums in `apply_pending_migrations` or drop the column (RT-MIG-5).
5. Pin the TZ tables to a single shared source (RT-MIG-4).
6. Fix the two false docstrings (`__init__` bootstrap; 010's version label).
