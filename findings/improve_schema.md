# SCHEMA — the seam completed (2026-08-23)

**Area chosen: the schema.** Every other area in the mandate list carries an
improve_*.md; the schema seam had a peer's uncommitted work in this tree
(core-owned `hypotheses` DDL, migration-shape tests) but no findings entry and
no committed state. Under exclusive file ownership I did not commit that
peer's files as mine; I built on their direction, fixed what was broken in
MINE-adjacent consumer code, and pinned the whole contract with tests.

## The finding — measured

Migration 013 moved `sport`/`market_type` off `hypotheses` into the
plugin-owned `hypothesis_sports_ext` side table. The uncommitted seam work
repaired four call sites using this SQL shape:

```sql
SELECT COALESCE(e.sport, h.sport) AS sport
FROM hypotheses h LEFT JOIN hypothesis_sports_ext e ON ...
```

**That pattern cannot work, and SQLite says so before any row is read.**
Column resolution is static: on the seam shape `h.sport` does not exist, so
the statement fails with `OperationalError: no such column: h.sport` even
when every `e.sport` is populated. Verified directly:

```
CREATE TABLE hypotheses(hypothesis_id TEXT PRIMARY KEY, name TEXT)   -- no sport
CREATE TABLE hypothesis_sports_ext(hypothesis_id PK, sport NOT NULL)
SELECT COALESCE(e.sport, h.sport) FROM hypotheses h LEFT JOIN ext e ...
-> sqlite3.OperationalError: no such column: h.sport
```

The peer's own untracked test suite caught exactly one instance of its own
pattern (`test_active_sports_query_works_on_seam_shape` FAILED,
"no such column: h.sport") and missed five more because each site's SQL was
inlined in production code with no test executing it.

Broken sites found by repo-wide grep for `COALESCE(e.(sport|market_type),
h.)` plus manual reads of each consumer:

1. `tools/autonomous.py:5176` — active-sports backtest resolution (caught by
   the peer's test).
2. `tools/self_repair.py:378` — premature-rejection detector.
3. `tools/hypothesis_generator.py:1414` — `_recent_theses`.
4. `api.py:3443` — draft-pattern scan.
5. `tools/autonomous.py:5606` — premature-rejection detector (`h.market_type`).
6. `tools/autonomous.py:6122` — top-hypotheses panel (`h.sport`,
   `h.market_type`) — this feeds the operator's per-hypothesis stats view.
7. `tools/autonomous.py:6954` — status panel top list (`h.sport`).
8. `tools/hermes_memory.py:676` — research-state block (`h.sport`,
   `h.market_type`). **This one puts text in front of the model on every loop
   iteration**; post-013 the whole block died inside its try/except and
   silently vanished from the prompt — the exact "looks like caution, is a
   dead end" failure mode MORNING_REPORT documents.

Blast radius: LOUD at sites 1–4 (exceptions), SILENT at 5–8 (bare-column
SELECTs wrapped in try/except return empty panels; the memory block returns
no research state). On any properly migrated DB the loop's resolution phase,
self-repair's requeue detector, the generator's dedup context, two operator
panels, the API draft scan, and the model's research-state injection were all
dead.

## What changed (commit c91d7c2)

Every site now branches on shape instead of pretending one SQL string can
serve both:

- Where the ext table exists (seam shape): `JOIN hypothesis_sports_ext e`
  and select `e.sport` / `e.market_type` directly. The join is INNER where
  the consumer only means sports claims (a general claim has no ext row and
  no sport — excluding it is correct, not lossy).
- Where it does not (welded pre-013): query the core columns directly, as
  before 013.
- `tools/schema/seam.py` — `has_ext_table(db)` / `is_welded_shape(db)`
  helpers and a comment stating the rule so the next consumer does not
  re-invent the broken COALESCE form.

## Before / after

- Before: on a fresh-seam DB, 8 consumer queries raise or silently return
  nothing (measured: the uncommitted tree's own test failed; my reproduction
  of each additional query failed identically).
- After: `tests/test_improve_schema_seam.py` executes the ACTUAL consumer SQL
  against both shapes — 24 passed, including 8 new parametrized
  consumer-query tests. Adjacent suites green: `test_hypothesis.py` +
  `test_build_b5_schema_split.py` + `test_prediction_store.py` (49),
  self-repair gate policy (15). Sports stays green; no gate arithmetic was
  touched.

## Honest caveats

- The welded-shape branch of each fixed query is exercised only by fixture
  DDL, not by a real pre-migration database — none exists outside backups of
  the workstation DB. The fixtures mirror migration 013's own down() shape.
- `hermes_memory._build_research_state` still hard-codes sports framing in
  its labels; the de-welding of its *content* was done by the memory/wiki
  pass (acbb5d0). Only the SQL here is mine.
- I deliberately did not commit the schema-seam peer's uncommitted edits to
  `tools/schema/core.py`, `plugins/sports/schema.py`, or
  `tests/test_build_b5_schema_split.py` — not my files. My tests run against
  whatever shape `ensure_schema()` + migrations produce, so they hold either
  way. If that work lands differently than its comments describe, the
  consumer-query tests will say so immediately.
