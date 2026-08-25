# MEMORY AND WIKI LAYER — improvement pass (build/memory-wiki)

**Area chosen: the memory and wiki layer** — tools/hermes_memory.py,
tools/knowledge_wiki.py, tools/memory_epistemics.py, and the one consumer
seam where its output becomes decisions (api.py `_wiki_task_short_circuit`,
`/wiki/stats`).

Why this one: every other AREAS item has an improve run on record (CLI ×2,
retrodiction/calibration, artifacts/sandbox, retrieval/synthesis/routing/
schema/checkpointing, edge quantification). This layer had none, despite
MORNING_REPORT flagging a verified trust-escalator defect here.

## Family hunted

**PATTERNS #2 — a fix lands in one copy while another keeps the bug**, with
**#3 (absence treated as success)** as the mechanism inside each copy, moving
in the forbidden direction (#6). The P4/R7 memory trust policy landed in
`record_learning` (admit_learning, replace-not-ratchet, ceilings) and in the
compile path (`_article_confidence` min-of-sources). Five sibling copies kept
the old behaviour:

| # | site | defect (before → after) |
|---|---|---|
| 1 | `knowledge_wiki._get_uncompiled_sources` learnings | raw stored confidence admitted — a pre-P4 row (migration 015 not auto-run) at **0.9** entered articles → clamped to **0.55**, labelled INFERRED |
| 2 | same, sessions | `"confidence": conf or 0.5`: NULL/0.0 sealed-session score manufactured up to **0.5** → enters at **0.0** |
| 3 | same, evidence (catalogue) | the last ungated path: any number ≥0.6 flowed in with no class, no ceiling → class read from row, unknown/NULL ⇒ INFERRED ceiling (**0.9 → 0.55**) |
| 4 | `write_lesson_article` | UPDATE **replaced** article confidence with the caller's number (self-reported 0.95 could raise an article sitting at 0.3); default 0.6 above the INFERRED ceiling → merges **downward only** (min), explicit `source_class=` opts into ceiling enforcement; undeclared writes unchanged (no silent re-ceiling of sports-loop values nobody asked to change) |
| 5 | `api._wiki_task_short_circuit` | `"confidence_score": top.get("confidence") or 0.5` raised an earned-nothing article back to 0.5 and let it **complete a research /task outright** (sim≥0.88, default ON) → refuses conf≤0/non-numeric, propagates true number + provenance class |

Also closed the gap pinned OPEN by `test_redteam_prov_memory_wiki`
("two INFERRED items and two PRIMARY items are indistinguishable"): sources
carry `provenance_class` through every ingestion path and articles persist
their weakest source class in a new `wiki_articles.provenance_class` column
(same `_safe_add_column` pattern as `source_task_id`; NULL = unclassified).
`/wiki/stats` now serves `get_write_stats()` — counters built so silent wiki
failures become loud were themselves unobservable.

The hermes side (record_learning/decay/reinjection) was verified already
fixed and well-tested; left untouched except tests.

## Tests

`tests/test_build_memory_wiki_policy.py` — 40 tests: the five sibling-copy
regressions, article class labelling (create/update merge-to-weakest),
lesson-write merge policy incl. same-value rewrite stability, short-circuit
honesty (real `api._wiki_task_short_circuit` with faked wiki/db), plus a
200-case×25-seed property sweep (PATTERNS method #1) over
(normalize → ceiling → clamp). All 13 policy tests were written FIRST and
failed against the pre-change code (family #7 discipline: watched them fail
for the right reasons). `test_redteam_prov_memory_wiki` updated: original
assertion kept (confidence arithmetic stays purely numeric), new resolution
test added.

## Before/after (this Mac)

| measure | before | after |
|---|---|---|
| max confidence an unverified guess reaches the compiler with | 0.9 (raw row) | 0.55, labelled INFERRED |
| NULL/0-confidence sealed session enters compilation at | 0.5 | 0.0 |
| unclassed catalogue evidence at 0.9 | 0.9, no class | ≤0.55, INFERRED |
| lesson write raising an existing article | possible (replace) | impossible (min-merge) |
| zero-earned article answering a `/task` | yes, at score 0.5 | refused → real pipeline |
| area suites (8 files) | 72 passed | 112 passed (+40) |
| full suite | 11,584 passed / 53 failed* | 11,598 passed / 53 failed |

\* failure list diffed byte-for-byte against a clean worktree at the
pre-session commit (a6e4467): identical 53, all peer-branch red-team/env
items. Sports/money suites green (tier0 kelly/sizing, hypothesis,
promotion gates, clv units, b1 clv gate, r5 edge, edge_confidence: 152).

Process note: the autosave daemon committed my in-flight work twice mid-run;
first baseline comparison accidentally used a worktree containing my own
edits — caught by checking HEAD's stat before trusting the numbers, redone
against a6e4467.

## Deliberately NOT done

- Did not clamp undeclared lesson writes to the INFERRED ceiling — callers
  write measured backtest/demotion summaries with code constants; silently
  re-ceiling recorded sports-loop values is a behaviour change nobody asked
  for. Declaring `source_class=` is the opt-in.
- Did not touch `hypothesis_generator.py`'s raw `INSERT OR REPLACE INTO
  wiki_articles` (hardcoded 0.8/0.5) — outside my files; it remains the one
  known bypass of the policy (leaves provenance_class NULL = unclassified).
- Did not apply time-decay at wiki admission (learnings enter on stored
  value); would near-starve learnings past ~2 half-life days and needs a
  design decision, not a drive-by.
- Did not wire `file_task_result` to seal-verify its session_id against the
  sessions table (it currently trusts the caller's confidence argument).
- Left `memory_epistemics.verify_learning_seal` as-is: fail-closed, no
  production caller passes seals today (everything honest-INFERRED).

## Honest caveats

- `provenance_class=NULL` means either legacy rows or wholly-sealed-session
  articles; consumers cannot distinguish those two yet. Numbers are still
  bounded by min-of-sources either way.
- The short-circuit decline shifts marginal tasks to the full pipeline (~5
  min each); that cost is the point — an unearned answer is worse than a
  slow one (PATTERNS #9).
