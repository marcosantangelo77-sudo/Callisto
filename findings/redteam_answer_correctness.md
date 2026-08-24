# Red Team: ATTACK THE ANSWER — Answer Correctness (run 2026-08-24)

Worktree: review-ox, branch review/ox-alpha-0824b. Baseline read:
`findings/one_real_question_run.json` @ ffeca37, PATTERNS.md.

## The new family: internally consistent, externally wrong

Every prior pass attacked an INTERNAL property — provenance intact, no
confidence raised, seal valid, resume faithful. Nothing verified
CORRESPONDENCE TO REALITY. The first real question this system answered was
sealed at PROBABLE / AFFIRMS asserting unemployment was LOWER in 2026 than
January 2023, when it is HIGHER (3.5% → 4.1%). Every internal signal passed:
provenance clean, seal valid, confidence not raised. The answer was simply
wrong, and the machinery certified it anyway.

This pass names the family and lands five reproductions per claim. All are
strict-xfail canaries in `tests/test_redteam_answer_correctness.py`
(9 xfail + 3 passing pins), except C4's note-surfacing, which was safe to fix
now (additive information only).

---

## C1. "A parent's stance reflects the parent's question." It does not.

engine.py `_run_inner`: `best_leaf = max(answered, key=lambda l: l.confidence)`
then `parent_stance = best_leaf.stance`. The parent's DIRECTION is whatever
leaf happened to win on MAGNITUDE — and magnitude tracks source class and
requirement satisfaction, not relevance to the root.

Task 180 covers the reported instance. Same-shape instances found:

1. **Off-topic winner sets direction** (`test_parent_stance_not_inherited_
   from_offtopic_leaf`). Reproduced END-TO-END against the live engine with a
   fixture FRED transport: leaf A "what was the Jan 2023 rate" scores 0.95,
   leaf B the actual 2026 comparison DENIES at 0.55. Sealed result: stance
   AFFIRMS on a root question whose truth is DENIES.
2. **Unprovable leaves give direction**
   (`test_unprovable_leaf_cannot_set_parent_direction`). A leaf that declared
   ITSELF `gap_kind=unprovable` still contributes stance+confidence to the
   parent when it wins. `result.gap_kinds` is carried as decoration beside the
   conclusion; nothing that decides `stance` reads it.
3. **Source class crosses topics** (`test_parent_does_not_inherit_leaf_source_
   class_across_topics`). `proposed = best_leaf.confidence`, and that number
   embeds the leaf's provenance ceiling. A PRIMARY-capped leaf about X hands
   PRIMARY-grade magnitude to a root question about Y; the parent wears a
   tier it never earned on its own question.

The same shape exists for **tier** (parent tier from `clamp_parent_confidence`
over the winning leaf only) and would exist for any future per-leaf property
wired through `best_leaf`. The general defect: *one scalar argmax selects
which child speaks for the parent, and every parent property then comes from
that child.*

## C2. Sub-answers compose into a FALSE parent answer

`test_all_true_children_compose_to_false_parent` (canary):

- Leaf 1: "lower in Jan 2023 than in 2026?" → AFFIRMS. **True** (3.5 < 4.1).
- Leaf 2: "higher in 2026 than Jan 2023?" → AFFIRMS. **True.**
- Root: "lower in 2026 than Jan 2023?" — **False**, but the composed parent
  direction is AFFIRMS.

Both children agree; both are correct; the conjunction asserts the opposite of
the truth. Nothing in decompose→answer→assemble ever compares leaf polarity
against ROOT polarity — decomposition can silently INVERT the comparison
("lower A than B" becomes "lower B than A") and no stage notices. This is the
exact mechanism that sealed the original wrong answer: the decomposer produced
a Jan-2023 leaf and a 2026-levels leaf but NO leaf carrying the root's
comparison itself; the parent then inherited direction from whichever leaf won.

Wider composition holes in the same class:
- quantified leaves over different windows/series conjoin without checking the
  windows overlap or the series match;
- stance composition treats {AFFIRMS, AFFIRMS} as agreement even when the two
  affirmations support OPPOSITE sides of the root;
- a DENIES leaf and an AFFIRMS leaf about inverted comparisons look like a
  contradiction to no one.

## C3. Two leaves querying the same series can silently get different data

In the recorded run, one leaf got the full UNRATE series and another got a
truncated window ("the visible series ends late 2025"). Paths where divergence
enters silently:

1. **No explicit window.** `query_builder._plan_fred` plans
   `series_observations(series_id, limit=120)` — no `observation_start`.
   Which observations come back depends on the endpoint's default slicing,
   not on anything the pipeline states. Two leaves hitting different cache
   entries, mirrors, or endpoint versions get different bytes and neither the
   gate nor the answer model knows the window differs.
   Canary: `test_series_window_is_explicit_and_recorded_on_the_answer`.
2. **Refinement mutates only free-text queries.** `refine_query` appends
   harvested tokens round-over-round for search sources; series-parameter
   calls never participate in refinement, so a leaf that refined its way to a
   different corpus looks identical in `trace.queries` shape but differs in
   effective data.
3. **Nothing records the window on the answer.** `LeafOutcome` carries answer/
   confidence/stance/classes — not WHICH slice of the series the evidence
   covered. The truncated-window leaf could only be diagnosed by a human
   reading prose. Canaries pin both planner determinism
   (`test_same_series_two_leaves_get_identical_query_parameters`) and explicit
   windows.

## C4. Asked ≠ answered — independence counting cannot tell

Reproduced live: single-source seal where `session.sources` lists all 21
registry specs, zero notes mention the 20 failures, `summary_dict()` reports
only `n_fetches`. The recorded run fetched courtlistener/fred/worldbank; only
FRED carried anything relevant; BLS (its only plausible corroborator) was down
— and NOTHING anywhere said so. Independence counting operates exclusively on
sources that ANSWERED (`trace.independent_keys`); it cannot express "the
independence bar was unmeetable because the other sources errored."

FIXED THIS PASS (safe: additive notes + one new summary field, moves no
number):
- engine.py now emits, per leaf: "sources asked but NOT contributing
  evidence: bls (error: …), … ; answer rests on ['fred']".
- `summary_dict()` gains `n_sources_answered` (distinct sources that actually
  contributed evidence).
- Pins: `test_single_source_seal_surfaces_which_sources_failed`,
  `test_summary_distinguishes_asked_from_answered`.

Note the honest behaviour discovered while testing: with
min_independent_sources=1 retrieval stops before ASKING further sources, so
"asked but failed" notes appear exactly when sources were consulted and
failed — which is the case the recorded run hid.

## C5. Arithmetic and comparison: wrong assertion from correct inputs

Reproduced end-to-end (`test_answer_may_not_contradict_its_own_computed_
comparison`): the model requests compute, the sandbox executes
`print(4.1 < 3.5)` → prints `False`, and the very next turn seals
"The rate WAS lower in July 2026 (4.1%) than January 2023 (3.5%)" at
PROBABLE/AFFIRMS. The sandbox output is stored as an artifact and appended as
evidence — and then ignored by every downstream check. There is no mechanism
comparing a computed boolean to the asserted stance.

Companion defect: `produced_quant = out.sandbox_status == "ok" or
bool(out.answer and re.search(r"\d", out.answer))` — ANY digit in the answer
(including a year: "in 2023 the rate was elevated") satisfies a
quant_required requirement. Canary:
`test_digit_in_prose_is_not_quantitative_evidence`.

Synthesis-layer numeric machinery has the same exposure on CORRECT inputs:
`detect_contradictions` picks each voice's value via `max(values, key=abs)`,
so (a) one document's context figure (pandemic peak 14.8%) manufactures a
MAJOR contradiction with an agreeing second source, and (b) percent-encoded
(0.035) vs plain-unit (3.5) statements of the SAME fact read as conflicts.
Both shown on agreeing inputs; both canaries strict-xfail
(TestNumericContradictionMachinery). A spurious contradiction caps good
answers at SPECULATIVE — mis-scoring in the conservative direction, but still
asserting a numeric disagreement that does not exist.

---

## What was changed in production

One thing, deliberately minimal:

| Change | Where | Why safe |
|---|---|---|
| Per-leaf "asked but not contributing" notes | engine.py assembly loop | Additive string into `result.notes`; reads trace rounds, writes no score |
| `n_sources_answered` field | engine.py `summary_dict` | New key; no consumer reads unknown keys |

Zero confidence numbers touched; full existing suite green after the change.

## What should NOT be fixed casually

- C1/C2 require a notion of "does this leaf bear on the root, and on which
  side" — i.e. polarity-aware decomposition validation and root-polarity
  checks at assembly. That is Task 180's design space; these canaries define
  the contract it must satisfy.
- C5 needs a compute-output↔stance consistency check (e.g. if compute ran and
  printed a bare boolean, stance must match it or the leaf refuses). Wiring it
  without a false-positive review would block legitimate runs; left as canary.

## Test inventory

tests/test_redteam_answer_correctness.py — 12 tests: 2 passing pins (planner
determinism, C4 surfacing ×2 incl. summary field) + 9 strict xfail canaries +
1 planner-window pin. All failing-before/passing-after for C4 confirmed
manually before the fix (single-source run previously produced zero failure
notes).
