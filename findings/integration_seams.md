# Integration Seams — Five Features, One Day (2026-08-24)

Branch: `redteam/integration-seams` (loop worktree).
Scope: the five features that landed independently — stopping rules
(`tools/pipeline/stasis_stop.py`), information gain
(`tools/pipeline/eval_information_gain.py` + gain gate in
`tools/pipeline/retrieval.py`), cross-run memory (`tools/pipeline/crossrun.py`),
parallel leaves (`tools/pipeline/engine.py`), gap classification
(`tools/gaps.py`) — attacked **in combination**, per seam analysis.

Reproductions: `tests/test_integration_seams.py` (component-level) and
`tests/test_integration_seam_engine.py` (engine-level). All were run against
the unmodified feature code and reproduce the stated behaviour.

---

## Verified sound (no defect)

**The known R4/R4b scratch-ledger hole is already closed.**
`_FetchRecorder` (engine.py:178) implements both `record_tool_result` AND
`record_gate_rejection`; the engine replays rejections in leaf order right
after each leaf's fetch records (engine.py:742-743). A rejected fetch under
parallel leaves lands on the real ledger exactly where the serial loop wrote
it. Test: `test_s3_r4_rejections_survive_parallel_leaf_fetches`.

**Seam 1, stasis-vs-gain-skip, does NOT misfire in the direction feared.**
The gain gate breaks with its own reason ("no candidate fetch could satisfy
any unmet declared requirement") *before* a second identical round can be
observed, so StasisStop never sees two identical fingerprints on that path —
`fired_at == 0`, stop_reason stays the gain gate's. The "barely-started run
halted as saturated" scenario requires round 2 to actually RUN and admit
nothing; when it runs and admits nothing, saturation is the truth.
Tests: `test_s1_*`.

**Seam 2, order-vs-skip divergence, is narrower than feared but real at the
boundary (F2 below).** When every fresh voice fits inside the budget, order
changes only which leaf-round fetched what; independence keys converge.
Divergence appears only when the budget binds before all fresh voices are
heard (see F2).

**Seam 3, parallel-leaf trace attribution, is correct.** Each leaf's
retrieval runs with its own scratch recorder and returns its own trace;
`self._crossrun_traces[q.question_id] = trace_q` is written in ordered
assembly (leaf order), keyed by that leaf's own question_id from its own
trace. No cross-thread sharing of retriever instances exists.
Test: `test_s3_*`.

---

## Confirmed defects

### F1 — All-junk rounds are invisible to the stasis rule AND mislabelled as a routing problem

**Seam:** stopping rules × fan-out exclusion × gaps.
**Repro:** `test_s4_all_junk_single_source_stop_reason_is_not_route_missing`
(retrieval level); engine variant in `test_integration_seam_engine.py::F1`.

One junk source: r1 rejects alpha → `excluded.add("alpha")` → r2 finds zero
routable specs → `break` with `"selected sources lack generic fetch routes"`.
Stasis never fires (r2 never ran). Two junk sources behave identically even
though r2 COULD have re-fetched them — the exclusion happens in the same
round-1 pass for both.

Two defects in one:
1. The message is false. Alpha HAD a generic route; the relevance gate judged
   its response irrelevant and the loop excluded it. An operator reading
   "lack generic fetch routes" goes to fix query authoring instead of the gate
   or the query terms.
2. The stop-reason taxonomy loses the case entirely: not "stasis:", not
   "sufficient:", not the terminator's — an undocumented fourth reason that
   downstream consumers cannot distinguish from a genuine planner gap.

### F2 — Crossrun reordering + gain gating changes WHICH sources a budget-limited leaf ever hears (run divergence)

**Seam:** cross-run memory × information gain.
**Repro:** `test_s2_memory_reorder_changes_which_sources_are_fetched`.

With `max_sources_per_leaf=2`, 3 sources, 1 round: plain order hears
{alpha,beta}; memory deprioritising beta hears {alpha,gamma}. Different
independence keys, different admitted bodies → different sealed tier/stance/
confidence inputs **for the same question class**, purely from remembered
order. The gain gate cannot repair this because within one round all kept
candidates look equally fresh — the skip logic never sees the source the
partition pushed out of the window.

This is ORDER-ONLY by design (gate rule 1), so it is arguably permitted —
but the docstring claims "A fully-deprioritised candidate list comes back
unchanged" and "sources remain reachable when the budget reaches them."
Under a binding per-round budget they are NOT reachable: the budget is spent
before the back of the list. The invariant claimed and the invariant
delivered differ. Byte-equality of conclusions across memory-on/memory-off
runs does NOT hold once budgets bind; the eval harness's identical-conclusion
bar (`eval_information_gain.py`) was only ever checked without a crossrun
view injected.

### F3 — Gain-gate skips + declared cannot-answer let a leaf exhaust its budget while the conclusion reports nothing about why

**Seam:** information gain × gaps.
**Repro:** `test_s5_gain_gate_exhausts_budget_without_gaining` and engine
variant.

Registry: alpha GOOD (only real voice), beta declares `cannot_answer` for the
question class, min_independent=2. Round 1 admits alpha and skips beta at
ingestion-planning? No — beta IS selected and fetched in r1 (cannot-answer is
only consulted by the GAIN gate, which starts at r2). From r2 on: beta is
gain-skipped (declared cannot-answer), alpha is gain-skipped (duplicate
voice) → "no candidate fetch could satisfy any unmet declared requirement".

The leaf then answers on ONE voice where TWO were required. The engine marks
it `unprovable` with reason `1 independent sources < required 2` — correct —
but the trace's own stop reason says no fetch could help, which is FALSE: a
second voice simply does not exist in this registry. Nothing distinguishes
"we stopped because more fetching provably cannot help" (true) from "we
stopped because the registry is exhausted and the requirement is UNMET" (the
actual situation, which should read as a coverage gap, not an efficient
stop). The gain_skipped audit trail exists on the trace but is not surfaced
into the leaf outcome or the conclusion text.

### F4 — Engine gap classification silently skipped for answered-on-partial-evidence leaves whose model answer comes back empty

**Seam:** parallel leaves (answer stage) × gaps.
**Repro:** `test_s5_answered_leaf_with_unmet_requirements_is_marked_unprovable`
and the empty-answer variants in `test_integration_seam_engine.py::F4`.

`engine._answer_leaf` (line 543): `if not fetches:` classify via
`classify_null_kind`; `elif reasons and out.answer:` mark unprovable.
There is NO branch for `fetches exist ∧ requirements unmet ∧ answer empty`.
That leaf gets `gap_kind = ""` — no verdict at all — and the run dies later
with the blanket refusal "every leaf came back unanswered", discarding the
per-leaf honest_null / retrieval_failure distinction the whole gaps module
exists to preserve. Concretely reproduced: 1 admitted fetch + junk elsewhere,
Manager returns `"answer": ""` → `leaf.gap_kind == ''`. The same fixtures
with a non-empty answer correctly produce `unprovable`. Whether a leaf reads
as "the literature is silent" vs "we could not look" therefore depends on
whether the answer model happened to emit a non-empty string — a
classification controlled by the least trustworthy component in the chain.

### F5 — Checkpoint restore drops `trace.rounds`: resumed runs lose error/skip provenance (cross-run record and coverage disclosure degrade)

**Seam:** parallel leaves (checkpoint restore path) × gaps × crossrun.
**Repro:** `test_f6_checkpoint_restore_drops_rounds_crossrun_record_degrades`.

`_trace_from_payload` (engine.py:1004) restores rejections, keys, queries,
stop_reason — but `_fetch_payload_dict` never serialises `rounds`, and the
restore never rebuilds them. Consequences, both demonstrated:

1. **crossrun record corruption:** live record says
   `{beta: {errored: 1}}`; restored record says `{}`. A chronic-erroring
   source looks like a source that was never tried; after DEPRIORITISE_MIN_RUNS
   such hollow records, memory will reorder on fabricated evidence (and the
   fragile flag never fires because errored counts are always 0 on resumed
   runs).
2. **coverage disclosure loss:** a mixed round (beta errored + alpha
   rejected) classifies honest_null WITH "NOTE some sources also errored,
   coverage may be partial" live; restored, the NOTE vanishes and the null
   reads as full-coverage silence. Kind happened to survive in the cases
   tested (because rejections restore), but the explanation — the part a
   researcher acts on — is laundered.

The comment at engine.py:782-788 claims records are recorded "for BOTH
branches ... so a resumed run's records match a live run's." They do not.

### F6 — Stasis stop fires while the terminator would have kept going: earlier halt, same verdict, different stop-reason string (accepted, documented)

Not a defect: with stasis wired at the engine level (opt-in today),
`stasis: round 2 changed neither independent sources nor admitted evidence`
fires one round earlier than the terminator's stall detection with identical
conclusion state (verified end-to-end: both paths seal `unprovable` 0.54 on
the same fixtures). Flagging only because the engine currently NEVER wires
StasisStop (`grep "stasis_stop =" tools/` → only the None default), so JOB 3
is dead code in production until someone opts in; when they do, F1's
mislabelling becomes the common case rather than an edge.

---

## Seam 5 — all-on vs all-off

Same question, same fixtures, every flag flipped:

| Configuration | Result |
|---|---|
| all OFF (single leaf, no memory, no gain, no stasis) | seals, SPECULATIVE 0.54, `[GAP: unprovable]` |
| all ON (2 leaves, gain gate) | seals, CORROBORATED 0.80, no GAP tag |

These are DIFFERENT decompositions so the divergence proves nothing by
itself; the controlled comparison is per-seam above. The load-bearing
finding: with everything enabled and a crossrun store attached, run 1 and
run 2 produced byte-identical notes/conclusions on stable fixtures (memory
changed nothing because nothing was chronic yet), and the stored records were
well-formed. Divergence enters exactly where F2 predicts: budget-binding +
deprioritisation.

## Summary table

| # | Seam | Verdict | Repro |
|---|------|---------|-------|
| F1 | stasis × exclusion | stop_reason mislabels judged-rejection as missing route; stasis blind to all-excluded rounds | test_s4_* |
| F2 | crossrun × gain gate | memory reorder changes heard-source set when budget binds; "reachable when budget reaches them" claim false | test_s2_* |
| F3 | gain gate × gaps | budget exhausted on unsatisfiable requirement; stop reason hides unmet-requirement exhaustion | test_s5_gain_gate_* |
| F4 | parallel answers × gaps | gap classification skipped when answer empty but fetches exist; verdict depends on model verbosity | test_s5_answered_* |
| F5 | checkpoint × gaps/crossrun | restored traces lose rounds → crossrun records hollow, partial-coverage note lost | test_f6_* |
| — | R4/R4b scratch ledger | already fixed (recorder captures rejections) | test_s3_r4_* |
| — | stasis × gain skip | safe: gain break preempts stasis fingerprinting | test_s1_* |

## Recommended fixes (not applied here — no confidence may be raised)

1. F1: set `trace.stop_reason = f"exhausted: all candidates excluded after "
   f"judged rejection"` (or similar) when `excluded` non-empty at the
   routable-empty break; reserve route-missing wording for genuinely
   unplannable sources.
2. F2: either make PlanningView.order_specs budget-aware or amend the
   crossrun docstring to state the budget-binding caveat explicitly.
3. F4: add the missing branch — `elif reasons and not out.answer:` classify
   via `classify_null_kind(trace)` (evidence existed; the answer failed).
4. F5: serialise `rounds` in `_fetch_payload_dict` and rebuild in
   `_trace_from_payload`.
