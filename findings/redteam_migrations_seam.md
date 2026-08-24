# RED TEAM — migrations & schema seam

**Surface chosen:** the schema and migration layer (`tools/migrations/`,
`tools/schema/engine.py`, `plugins/sports/schema.py`) plus the seam between
the two seal verifiers (`agp` vs `tools/memory_epistemics.py`) that the
migration/ingestion consumers sit on.

**Why this surface:** unattacked ground. The twelve prior passes clustered
four-deep on resume/checkpoint and never touched migrations, despite
MORNING_REPORT flagging "Migrations 013 and 015 have never been run [against
the real DB]" and the schema seam being the newest structural change on
master. Highest concentration of never-executed code = highest yield for the
"what calls this verification?" question.

**Method:** cross-module differential (family 2 hunting) plus
absence-as-success probing (family 3) — for every check in the layer I asked
who calls it and what happens when its input is missing, then diffed pairs of
components that must agree (two verifiers, two schema sources of truth,
migration-vs-read-time decay). Property sweeps were used where a numeric
invariant existed (015 decay vs `memory_epistemics.decay_confidence`: max
divergence 0.0 over 500 random cases — that copy agrees).

**Families hunted:** #1 (verification layer that never runs), #2 (fix lands
in one copy, another keeps the bug), #3 (absence treated as success).
All three hit again.

---

## RT-MIG-1 — Migration checksums are written and never read (CRITICAL, family 1)

`runner._migration_source_checksum()` stores SHA-256 of each applied
migration's source in `schema_migrations.checksum`. Its own docstring
promises: *"so a later audit can detect 'someone edited 002_add_archived
after it was applied.'"* **No such audit exists.** Grep over the entire
production tree shows zero readers — the column is write-only. A tampered or
edited applied migration is invisible to `apply_pending_migrations`, the only
public entry point.

Repro: `tests/test_redteam_migrations_seam.py::TestChecksumNeverVerified`.
This is W5/C1/A6 shape for the fourth+ time: the check exists, looks
authoritative, and cannot fire.

## RT-MIG-2 — ensure_schema() still creates the welded schema on fresh DBs (HIGH, family 2)

Migration 013 removed `sport NOT NULL` from `hypotheses` — but the plugin DDL
(`plugins/sports/schema.py:194`) still declares the full welded table.
Fresh-DB order is: `ensure_schema()` (welded CREATE succeeds) → runner → 013
sees the weld and re-migrates a table it just created. It works only because
api.py happens to run both, in order. Any caller of `ensure_schema` alone
(e.g. `scripts/import_ncaaw_closing_lines.py:204`) gets a DB where the
domain-general lifecycle is structurally impossible: inserting a non-sports
claim fails with `NOT NULL constraint failed: hypotheses.sport`.

Repro: `TestFreshDbWeldResurrection` (both tests). The plugin DDL is the
uncorrected second copy of the rule 013 fixed.

## RT-MIG-3 — Two seal verifiers disagree; the wiki gates trust on the lenient one (CRITICAL, family 2)

`AGPSession.verify_seal` accepts the public SHA-256 digest even under a keyed
regime ("legacy compat"). `memory_epistemics.verify_seal_method` correctly
classifies that digest as `legacy-fallback`/forgeable and refuses it — the
P4 docstring claims agreement is "pinned by tests," but the pinned property
only covers *tampered* payloads; on intact payloads they deliberately diverge,
and the divergence direction matters:

- `knowledge_wiki._get_uncompiled_sources` uses **agp's** verdict as its
  admission gate, and on admission keeps confidence **uncapped**
  (`provenance_class=None → keep stored confidence`).
- The default deployment is **unkeyed**: `CALLISTO_SEAL_KEY` appears nowhere
  in `.env.example`, launch/supervisor scripts, or configs. Seals are plain
  SHA-256 — forgeable by anyone with DB access (red-team R5 knew; the fix
  landed in memory_epistemics only).

Demonstrated end-to-end: forge a digest over a fabricated conclusion at
confidence 0.95, and the wiki admits it uncapped — while the identical bytes
entering through `admit_learning` would be collapsed to INFERRED and capped
at 0.55. Same evidence, two trust regimes, selected by which consumer reads
it. Repro: `TestSealVerifierDivergence`.

## RT-MIG-4 — record_learning computes provenance and throws it away (HIGH, family 1)

`hermes_memory.record_learning` calls `admit_learning`, logs the resulting
class, then UPSERTs only `(key, value, learned_at, confidence, source)` — on
both the coordinator path and the direct path. The `source_class` /
`provenance_seal` columns created by migration 015 are **never written by any
production caller** (grep confirms zero writers). Consequences:

- Every row stays `source_class=NULL` forever; 015's ceiling clamp can never
  classify rows written after it runs.
- `annotate_for_reinjection` reads NULL as INFERRED (fail-closed, fine), but
  the seal-gated upgrade path exists only in tests — the machinery 015 built
  is dead code from the write side.

Repro: `TestProvenanceNeverPersisted`.

## RT-MIG-5 — Migration 015 down() NULL-poisons post-migration rows (MEDIUM)

`down()` restores confidences with
`SET confidence = (SELECT ... FROM backup WHERE key = ...)`. Rows created
*after* the migration have no backup row, so the correlated subquery yields
NULL and — since the column is nullable — their confidence becomes NULL.
Verified live: a row inserted at 0.6 comes back as `None`. NULL then silently
drops out of `WHERE confidence >= 0.5` reads and forces min-of-sources wiki
article confidence to 0.0. The rollback path of a migration whose purpose was
repairing corrupted trust corrupts data itself. Repro:
`TestMigration015DownNullPoisoning`.

## RT-MIG-6 — Migration 004's orphan sweep is blind when a parent id is NULL (LOW/MEDIUM, family 3)

The count-and-delete both use `fk NOT IN (SELECT fk FROM parent)`. SQLite
TEXT primary keys accept NULL; one NULL `hypotheses.hypothesis_id` makes
`NOT IN` never TRUE — zero orphans counted, zero deleted, log reports a clean
database while real ghosts survive. Repro:
`TestOrphanCleanupNullBlindness`. Fix shape: use `NOT EXISTS` or filter
`parent.fk IS NOT NULL`.

---

## Verified-clean (negative results, for the record)

- **013 weld removal** — attacked hard: NULL-PK rows, pre-existing ext rows,
  duplicate names, FK repair regex, down() orphan guard. The row-parity aborts
  hold; the only wrinkle found was that NULL-PK rows survive into ext (SQLite
  allows NULL TEXT PKs) without violating parity — cosmetic, not data loss.
- **Runner concurrency** — lock release before apply looked like a race, but
  the per-migration BEGIN IMMEDIATE + version re-check inside the tx closes
  it; the existing concurrent test is sound.
- **Decay agreement** — `m015._decay` vs `decay_confidence`: 0.0 max
  divergence across 500 random (confidence, age) pairs.
- **Second-startup resurrection** — after 013 lands, re-running
  ensure_schema's DDL does not resurrect the weld (IF NOT EXISTS no-op), and
  the engine's 20260421/22 rebuild gates see `'paused'` present and skip.
  The fresh-DB path (RT-MIG-2) is the live hole, not restarts.

## What would fix these

1. Add a checksum verifier pass at startup (warn/fail on mismatch) — or delete
   the column and its docstring promise.
2. Move the domain-general `hypotheses` DDL out of the plugin into core (or
   make the plugin's copy conditional); one source of truth per table shape.
3. Route the wiki's session gate through `verify_seal_method`'s strict
   verdict and cap admitted legacy/unkeyed seals at the INFERRED ceiling.
4. Persist `source_class`/`provenance_seal` in both record_learning upserts.
5. `COALESCE((SELECT ...), confidence)` in 015.down(), or restrict the UPDATE
   to keys present in backup.

Tests: `tests/test_redteam_migrations_seam.py` — 10 cases, all passing
against current master code, each asserting the DEFECTIVE behavior so any fix
flips them loudly.
