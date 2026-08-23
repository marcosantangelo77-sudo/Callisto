# PROVIDER/ROUTING LAYER — improvement pass (build/cli-front-door)

**Area chosen: the provider/routing layer** (`inference.py` routing half —
ProviderRouter, `tools/routing/policy.py`, `tools/routing/scores.py`,
the retrodiction→routing bridge `write_routing_scores`,
`scripts/run_retro_batch.py`).

Why this one: every other area is taken — CLI (×2), AGP core, hypothesis
lifecycle, edge sizing, artifacts/sandbox, retrodiction/calibration harness,
autonomous loop are all covered by prior improve passes; memory/wiki and the
source registry carry a peer's uncommitted work in this tree. Routing has no
improve pass, and a red-team canary file landed against it that nobody had
fixed.

## The area's purpose

NEXT.md's multi-model section: different models in different roles,
cross-provider ensembles, re-verification on upgrade — all of it rests on
routing being *empirical*. W2 built the machinery: an append-only
per-(role, model) Brier score store and a Thompson-sampling policy with cost
awareness and honest basis labels ("configured / sparse / provisional /
measured"). This pass asked: does the measurement loop actually close?

## Findings and fixes

### 1. W8 — routing pooled task_classes within a role (fixed)

`ThompsonRoutingPolicy.decide(role, candidates)` compared candidates on their
whole role record regardless of what kind of call was being routed.
Measured before/after on a discriminating scenario (A measured only on
research_synthesis at Brier 0.20; B measured only on classification at 0.05;
routing a SYNTHESIS call over 100 seeds):

- before: **B wins 100/100** — a classification specialist routes every
  synthesis call on evidence it never earned;
- after: **A wins 73/100** (B still explored via its wide chance prior, which
  is correct Thompson behaviour for an unmeasured slice).

Fix: `decide()` takes `task_class`; each candidate is judged on its
(role, task_class) slice; an empty slice means UNMEASURED for that call — wide
prior draw, inherits nothing. A model being great at classification is simply
not evidence about how it synthesises. `ProviderRouter.route_order()` now
forwards `task_class`. Calling without `task_class` preserves the old pooled
behaviour exactly (pinned by test).

Design note: I first implemented empty-slice → role-wide fallback. That was
wrong — it reintroduces the leak through the back door (B still won 45/50).
Unmeasured must mean unmeasured.

### 2. W7 — batch reruns duplicated score rows (fixed)

`write_routing_scores` appended unconditionally, so a resumed/replayed batch
doubled n, weakened shrinkage toward the chance prior, and inflated basis
labels ("sparse" → "provisional" on identical evidence). Fix: dedupe on
(role, model, task_class, question_id); reruns append 0. Correcting a value is
an explicit `--fresh-scores` run (new flag), never a silent double-count —
consistent with the store's append-only honesty contract.

### 3. The loop doesn't close yet — documented, partially addressed

The router routes per (role, task_class) keyed by `endpoint.model`, but the
only production writer of scores records under hardcoded
`role="pipeline", model="hermes-cli"`. Names that never match a lookup key
mean empirical routing could never fire off real data even when enabled.
This pass: `scripts/run_retro_batch.py` now derives the model identity from
the actual researcher model instead of a bare literal, prints it alongside
the observation count so mismatches are visible, and the contract is written
into `write_routing_scores`' docstring. What remains open for the next run:
per-ROLE scores from a live RouterModel pipeline (each AGP role recording
under its own role + served endpoint.model), which needs the pipeline to
surface which tier served each judgment — `complete()` already returns it.

## Verification

- New tests: tests/test_improve_provider_routing.py — 8 passed (W8 scoping ×4,
  W7 dedupe ×3, router-forwards-task_class ×1).
- Existing suites green: test_build_w2_empirical_routing (21),
  test_build_i4_retro_batch + p1_retro + retro_hardening,
  test_tier5_serving_provider_router — 73 passed combined.
- Sports regression (rule 4): 226 sports/odds/kelly tests +
  schema-split suite all pass.
- Red-team canaries now fire as intended: w7 and w8 canaries FAIL (defects
  fixed); w4 and floor_conf failures pre-date this pass and live in
  peer-owned files (pipeline engine/checkpoint, agp.thresholds) — left alone
  under exclusive file ownership.
- Pre-existing failures confirmed NOT mine: test_backtest_e2e ('sport'
  KeyError) and test_build_w6_market_probability_planning fail identically
  with my changes stashed.

Not adopted from outside: looked for maintained bandit libraries (e.g. mabwiser)
— unnecessary; the ~200-line Thompson policy with cost exchange-rate and honest
basis metadata is more tailored than any generic library, and adding a dependency
for it would be regression by complexity.
