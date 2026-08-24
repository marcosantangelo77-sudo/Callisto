# MEMORY & WIKI LAYER — improvement pass (build/memory-wiki-improve)

**Area chosen: the memory and wiki layer** — tools/hermes_memory.py,
tools/memory_epistemics.py, the knowledge_wiki.py compile-ingestion seam,
and the loop's write paths into memory (tools/autonomous.py, two sites).

Why this one: no improve run has owned it. CLI ×2, artifacts/sandbox,
retrodiction/calibration are covered; retrieval, synthesis, checkpointing,
routing and schema had build waves. This layer decides WHAT THE LOOP SEES on
every iteration — instance4.md called it the trust escalator's home — and its
P4 fix wave turned out to have landed half-inert. Also: Callisto was not
running this session; no live system was touched.

## FAMILY HUNTED: #1 — a verification layer that never actually runs

PATTERNS.md asks the highest-yield question: *for every verification layer,
what calls it, and what happens when its input is missing?* Applied to the
memory trust pipeline (write → admit → store → read → reinject/compile),
FIVE instances surfaced in one sweep. Every one is a check or an epistemics
fix whose output goes nowhere — none can overstate, but three silently
undermine guarantees the code claims to enforce:

1. **Decay computed, then clobbered.** `_build_learnings` computes
   `decay_confidence()` per row and hands it to `annotate_for_reinjection`
   as `effective_confidence` — which overwrote it with the UNDECAYED stored
   value (memory_epistemics annotate, final assignment). The P4 wave's
   centerpiece ("nothing is monotonic; unobserved learnings lose standing")
   never reached a prompt, nor `trim_learnings_for_context`'s ranking, which
   sorts on the same clobbered field. Measured repro: a 60-day-unobserved
   learning at stored 0.55 displayed **0.55** in context; earned standing
   was **0.05**.

2+3. **Two dead write paths.** `await get_hermes_memory()` at
   autonomous.py:4547 (pipeline-validation findings) and :7791
   (system-watchdog findings). The factory is not a coroutine → TypeError
   every call → swallowed by bare `except Exception: pass`. Those findings
   have NEVER been recorded to Hermes. Verified by direct repro of the await.

4. **Provenance evaporates between write and read.** `admit_learning`
   computes/honors the provenance class (with keyed-seal verification);
   migration 015 added `source_class`/`provenance_seal` columns for exactly
   this — and `record_learning` never wrote them. Both read paths defaulted
   every row back to INFERRED. A seal-verified SECONDARY learning was
   reinjected as an unverified guess with a 0.55 ceiling. The check ran;
   its verdict was discarded.

5. **The wiki-admission guarantee is false.** memory_epistemics docstring:
   "the wiki's >= 0.5 admission gate therefore cannot be reached by an
   unverified guess alone." The INFERRED ceiling is 0.55 — ABOVE the gate.
   Measured repro: a claude-source guess claiming 0.9 stores 0.55 and
   compiles into the wiki, carrying NO provenance marker, indistinguishable
   from evidence. The wiki side of the documented escalator
   (self-report → ratchet → wiki → prompt) remained open.

## What changed

- **memory_epistemics.annotate_for_reinjection** preserves a caller-supplied
  `effective_confidence`, clamped to the class ceiling; falls back to stored
  confidence when absent (get_actionable_learnings path unchanged).
- **autonomous.py** — both illegal awaits removed (`hm = get_hermes_memory()`).
- **hermes_memory**: `_ensure_tables` now creates fresh tables WITH
  `source_class`/`provenance_seal` and lazily ALTERs legacy tables (guarded;
  converges with migration 015 in either order — relevant because 015 has
  NOT been run on the workstation DB). `record_learning` persists the
  admitted class + seal hash on BOTH write paths (direct and
  WriteCoordinator). Both reads SELECT the persisted class and pass it
  through annotation.
- **knowledge_wiki._get_uncompiled_sources** — learning admission now
  requires CURRENT standing (`decay_confidence` ≥ LEARNING_ADMISSION_GATE)
  AND, for INFERRED-class learnings, re-observation
  (occurrences ≥ LEARNING_MIN_OBSERVATIONS = 2). Admitted sources carry
  `provenance_class`, and compile-input confidence is the decayed,
  ceiling-clamped standing — never the historical peak.
- tests/test_tier3_epi_wiki_ingestion docstring updated to the new pinned
  behavior (family-#4 hygiene: docs must match behavior).

## Tests

tests/test_build_memory_wiki_improve.py — 13 tests, all failing-first
(verified: 9 failed against the pre-fix tree before any production edit).
Covers: decay surviving reinjection end-to-end through `_build_learnings`
(prompt text asserts the DECAYED percentage), the no-illegal-await source
pin, sealed→SECONDARY / unclaimed-PRIMARY→INFERRED round-trips, lazy legacy
schema upgrade, WriteCoordinator-path SQL parity, and all four wiki admission
rules (one-shot rejected, re-observed admitted with marker, stale decays out,
higher class exempt from corroboration).

## Before/after

| measure | before | after |
|---|---|---|
| prompt-facing confidence, 60d-unobserved learning (stored 0.55) | 0.55 | 0.05 (earned standing) |
| trim ranking input | undecayed | decayed |
| pipeline-validation / watchdog findings reaching Hermes | never (TypeError, swallowed) | recorded |
| seal-verified class persisting to read-back | never (columns written by nothing) | always |
| one-shot unverified guess compiling into wiki | yes, unmarked | rejected |
| wiki learning compile confidence | stored peak | decayed standing, ceiling-clamped |
| area tests | — | +13 |
| full suite (this Mac) | 53 failed / 11,557 passed | same 53 failures, byte-identical list / 11,570 passed |

The 53-failure set is verified pre-existing: diffed against a clean worktree
at merge-base a6e4467 (xgboost/libomp dlopen collection errors excluded on
both sides). Sports/money regression suites green: hypothesis, promotion
gates, tier0 kelly/sizing, clv units, r5 edge, b1 clv gate, edge_confidence —
152 passed.

## Honest caveats

- The no-await pin is a source-scan test, not behavioral: driving the real
  validation/watchdog phases needs the full loop harness. The defect IS the
  await spelling (TypeError swallowed), so the pin targets exactly that.
- Learning corroboration counts `occurrences`, which increments on every
  upsert including rewrites of the same key — stable-key writers (e.g.
  self_repair_*) accumulate observations quickly. Accepted: those ARE
  recurring observations; a distinct-key guess remains one-shot.
- Migration 015's stored-decay pass and my runtime read-time decay now both
  act on the same column; they agree by construction (same half-life/floor,
  pinned in p4 tests), so double application only converges faster.
- Unread Hermes messages are still never marked read in production
  (get_unread_messages has zero callers — ARCHITECTURE_MAP dead-code list):
  the messages section re-shows up to 5 oldest unread forever. Left as-is;
  changing delivery semantics (mark-on-build vs explicit consume) is a
  product decision, flagged here for the next run on this area.
