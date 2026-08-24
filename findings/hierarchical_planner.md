# Hierarchical planning for Callisto — gap-triggered leaf re-planning

Date: 2026-08-23 · Branch: `build/hierarchical-planner` (pushed)
Worktree: `~/callisto-wt/data`

## DERIVED FILES (licence obligation, stated up front)

SkyworkAI/DeepResearchAgent is MIT-licensed,
Copyright (c) 2025 AgentOrchestra. The following file is DERIVED from
their `src/agent/planning_agent.py` design and carries the notice:

- `tools/pipeline/replan.py` — the re-planning decision module. Its
  *architecture* is adapted from DRA's PlanningAgent/PlanDecision split
  (planner consulted with execution history; structured decision; loop
  owned by the caller). No code is copied verbatim; the copyright notice
  is preserved in the file header as required.

Everything else touched (`tools/pipeline/engine.py`, tests, eval script)
is Callisto's own wiring and contains no DRA-derived material.

## JOB 1 — side-by-side: what their planner does that ours does not

| Capability | DRA (`planning_agent.py` + bus) | Callisto (`engine._decompose`) |
|---|---|---|
| Decomposition depth | Unbounded rounds: planner re-consulted every round until `is_done`, up to `max_rounds` | ONE call at stage 1; output is the whole plan |
| Re-planning on failure | Core feature: planner sees `execution_history` + previous round's results and writes an `analysis` evaluating them before dispatching again | None. A failed leaf stays failed; `gaps.py` classified WHY but nothing acted on it |
| Sub-agent specialisation | Planner picks a named agent per subtask (`SubTaskDispatch.agent_name`) from the registry contract | One generic researcher path per leaf; specialisation only via `question_type` → source selection |
| Budget allocation | Per-round: each round's `dispatches` are its budget; planner can stop early (`is_done`) or keep spending | Static: `max_rounds=3` retriever loop, fixed leaf count ≤5 |

The single biggest gap for OUR architecture: **nothing consumed the gap
signal**. DRA's advantage is not "more agents", it is that the plan reacts
to what came back.

## JOB 2 — the smallest structural improvement

**Gap-triggered one-shot leaf re-planning** (`tools/pipeline/replan.py`,
wired in `engine.run` / `_maybe_replan_leaf`).

After a leaf completes, if its structured gap says the PLANNER can fix the
failure, the model gets ONE extra consult (same `Architect` role, same
decompose JSON contract) to produce a REPLACEMENT sub-question. The
replacement runs through fetch+answer unchanged.

Trigger rule — deliberately narrow:

- FIRES on `retrieval_failure` where either the obstacle is
  planner-actionable (`no_query_issued`, `no_adapter`) or the search never
  really happened (0 admitted AND 0 gate-judged responses) yet some
  plausible holder is reachable (untried, has its API key, not declared
  unable to hold it).
- NEVER fires on honest nulls (the retriever's refine loop owns those),
  unprovable claims (that is our own evidence bar — gates territory),
  access obstacles (`no_api_key`, `rate_limited`, `paywalled` — owner
  actions, not research actions), or leaves that answered.
- Budget: one re-plan per leaf per run; every decision lands in
  `PipelineResult.replan_events` + `notes`.

Constraint compliance:

- The planner decides WHAT to research, never HOW CERTAIN: no confidence,
  tier, or requirement information enters the re-plan prompt
  (tested); replacement questions are built by `_build_question_from_spec`,
  the exact construction rules of stage-1 decompose.
- No code path raises confidence: all scoring (min(estimate, ceiling),
  requirement gates, adversary) runs unchanged downstream
  (tested: `test_no_code_path_raises_confidence`).
- Incidental fix found while measuring: engine fan-out was
  `max_sources_per_leaf=3` while selection returns up to ~7 eligible
  sources; later-ranked sources (gdelt, kalshi) could never be tried
  before the terminator fired. Raised to 6.

## JOB 3 — golden-run proof

`scripts/eval_replan_golden.py` runs every golden-corpus case plus one
conversion case through the FULL pipeline twice (re-plan monkeypatched OFF
vs ON, identical scripted model). Full JSON:
`findings/replan_golden.json`.

Results (28 cases):

- **Conclusions**: identical on all 26 cases where the re-plan did not
  fire (asserted programmatically, qid-keyed fields excepted). Zero
  regressions anywhere.
- **Conversion (R1)**: a leaf whose selected sources were unreachable —
  previously refused ("every leaf came back unanswered") — is re-planned
  once, re-aimed at agency-rules sources, and seals at PROBABLE 0.55.
  Fetches 0 → 1.
- **Fetch cost**: 15 total fetches OFF vs 16 ON across the whole corpus —
  the re-plan costs exactly one extra retrieval pass ON THE TWO CASES
  WHERE IT FIRED, zero elsewhere.
- **Elapsed**: mean 0.036s per case in both arms (fixture transport;
  live-model cost would be one extra Architect completion per fired case).

## Test status

- New: `tests/test_build_replan.py` — 14 tests (membership rule table,
  degenerate-output rejection, no-confidence-leak prompt check, engine
  integration: fires-once, honest/access-gap suppression, clamp test).
- Full suite: 34 failed / 11218 passed — byte-identical failure set to
  the pre-change baseline (all pre-existing: redteam_artifacts_store,
  backtest_e2e, redteam_confidence_laundering, lifecycle_claim,
  prop_scanner; ml files excluded at collection as before).

## What this does NOT do (deliberately)

- No full multi-round hierarchical loop (DRA's `max_rounds` planner
  sessions): our pipeline is one-pass-with-one-recovery per leaf. The
  checkpoint/seal machinery assumes a bounded stage set; a real
  planner-round architecture is a larger change and unevaluable under the
  same-conclusion constraint.
- No specialised sub-agent roster: role specialisation remains
  `(role, task_class)` routing in the store; the re-plan changes the
  question, not the worker.
