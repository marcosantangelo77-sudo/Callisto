# PROVIDER/ROUTING LAYER — improvement pass (build/cli-front-door)

**Area chosen: the provider/routing layer** (`inference.py::ProviderRouter`,
`tools/routing/policy.py`, `tools/routing/scores.py`, and the seam where
`tools/retrodiction/batch.write_routing_scores` feeds measurements back into
routing).

Why this one: CLI was covered twice; the runs before that took AGP core,
retrodiction/calibration, edge sizing, artifacts/sandbox, the hypothesis
lifecycle, and the autonomous loop. Memory/wiki and the source registry carry
peers' uncommitted work in this tree, so both were off-limits under exclusive
file ownership. Routing is NEXT.md's multi-model role-assignment item — the
capability "nothing else has" depends on model choice being MEASURED, not
guessed — and its measurement loop had never been closed end to end.

## What was wrong — measured

W2 landed the machinery (append-only score store, Thompson policy, cost-aware
selection) but three defects broke the loop between *measuring* models and
*routing on* those measurements:

### W8 — task_class pooling poisoned every decision

`ThompsonRoutingPolicy.decide(role, candidates)` pooled ALL task classes
under a role. A model measured only on `classification` (say Brier 0.05)
would win `research_synthesis` draws it had never answered one question of.
Reproduced deterministically pre-fix: 100/100 draws to the specialist
(`tests/test_improve_provider_routing.py::TestTaskClassScoping`).

Fix: `decide()` now takes `task_class`; each candidate is judged on its
`(role, task_class)` slice via `_records_for()`. An empty slice means the
candidate is UNMEASURED for this call — wide chance-centred draw, explored,
never trusted, inheriting nothing. `inference.py:1175` passes the real
task_class through. When no slice exists anywhere the behaviour is unchanged;
with empirical routing disabled (the default) nothing changes at all.

### W7 — batch reruns double-counted observations

A resumed or rerun retrodiction batch replayed checkpoints and appended
duplicate rows to `ModelScoreStore`: n doubled, shrinkage weakened, and the
honesty basis label inflated ("sparse" → "provisional" on identical
evidence). Fix: `write_routing_scores` dedupes on
`(role, model, task_class, question_id)`; correcting a value is an explicit
`--fresh-scores` delete-and-rerun (new flag in `scripts/run_retro_batch.py`),
never a silent double-count. The append-only store itself is untouched.

### Loop closure — scores recorded under names the router cannot look up

`run_retro_batch.py` hardcoded `role="pipeline", model="hermes-cli"` while
the router keys candidates by `endpoint.model`. Measurements written under a
name no route ever queries are decoration, not science. Fix: the script now
derives the model identity from the researcher's actual model object
(`getattr(factory.model, "name", ...)`), so recorded identity matches routed
identity by construction.

## Before/after

| | before | after |
|---|---|---|
| synthesis call, specialist rival measured only on classification | specialist wins 100/100 draws | specialist wins ~0 draws (unmeasured for synthesis); true synthesis record decides |
| batch rerun of 30 scored questions | +30 duplicate rows, basis inflates sparse→provisional | +0 rows |
| recorded model name vs routed name | `"hermes-cli"` literal vs `endpoint.model` | identical by construction |

Tests: `tests/test_improve_provider_routing.py` (new, 8 passing) covers slice
isolation both directions (specialist kept out of synthesis AND synthesis
specialist kept out of classification), unmeasured exploration, cost
interaction, and dedupe. The peer's red-team canaries for W7/W8 in
`tests/test_redteam_pipeline_wiring.py` were flipped from bug-pins to fixed-
behaviour pins (their comments invited exactly that). Full routing suite:
40 passed.

## Not mine, left alone

Two canaries in the same red-team file fail on this branch for reasons
OUTSIDE routing: `test_w4_resume_counts_own_sandbox_as_second_independent_source`
(engine leaf counting — a peer has engine.py changes in flight) and
`test_neg_floor_conf_never_raises_any_clamp_input` (imports `floor_conf`,
which exists only on `build/dd-decomposition-diversity`, unmerged here).
Both belong to that branch's pass; touching them from here would collide
with its owner.

## What a careful designer might still change (not built this run)

- `EST_INPUT_TOKENS/EST_OUTPUT_TOKENS` are constants (1000/500); roles differ
  by 10x in token volume, which distorts cost comparison per role. Worth
  making per-role config once measurements exist to justify it.
- `ModelScoreStore.summary()` re-reads the whole JSONL per decide(); fine at
  current scale (<10k records), worth an index if batches grow 100x.
