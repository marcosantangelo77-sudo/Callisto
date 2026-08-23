# HYPOTHESIS LIFECYCLE — improvement pass (build/cli-front-door)

**Area chosen: the hypothesis lifecycle** (`tools/hypothesis.py`,
`tools/hypothesis_generator.py`, `tools/resolvers/`).

Why this one: CLI was covered twice, and the four runs before this one took
AGP core, retrodiction/calibration, edge sizing, and artifacts/sandbox. The
lifecycle is the system's central abstraction — BUILD_MANDATE §2 calls
`draft → backtesting → paper_trading → live → retired` "a strong abstraction
for any falsifiable claim" — and no improve pass had owned it.

## The finding — measured

BUILD_MANDATE queue item 1 called OutcomeResolver "the single highest-value
change in the repo": it is what turns a betting engine into a research engine.
The seam landed half-built.

The READ side exists and is good: `SqlitePredictionResolver` reads two clean
domain-general tables (`predictions`, `outcomes`), normalises any domain's
outcome vocabulary onto positive/negative/indeterminate, and feeds Brier,
calibration bins and the inheritance rule without a sportsbook present.

But:

1. **Those tables existed in NO migration.** `grep -rln "CREATE TABLE.*predictions"`
   over the repo returned exactly three hits: `tools/golf_masters.py`
   (masters-specific), `plugins/sports/schema.py` (sports), and
   `tests/test_build_b1_resolvers.py`. The generic tables were created only
   inside a test's ad-hoc SQL. Migration runner applies 001→015; none of them
   makes the resolver's read path real against a deployed database.
2. **Nothing wrote to them.** No production code path records a prediction or
   resolves an outcome for a non-sports claim. `PredictionStore` did not exist;
   `model_registry.py` (JSON-log predictions) has zero consumers outside its
   own test file.

Consequence: a Bitcoin hash-rate claim or a materials-science forecast could
enter the lifecycle only via an in-memory resolver whose evidence dies with
the process, or by hand-writing DDL against the live database. The durability
gap MORNING_REPORT flagged for `ProvenanceLedger` ("memory-only") applied to
the lifecycle's own evidence too. The general engine's track record could not
accumulate — which is property 4 of BUILD_MANDATE ("it knows how accurate it
is"), the product itself.

## What changed (commit 3a8ab07)

1. **Migration `016_generic_predictions.py`** — formalises the exact table
   shapes the resolver already reads:
   `predictions(id, claim_id, event_id, predicted_prob, context_key, created_at)`
   + `outcomes(prediction_id PK/FK UNIQUE, resolved_outcome, payoff, resolved_at)`.
   `claim_id` joins hypotheses **by convention, not FK** — claims may live
   outside that table, and a hard FK would weld generality back to sports.
   Verified: full chain 001→016 applies cleanly on a fresh DB through
   `apply_pending_migrations`; both tables land.
2. **`tools/resolvers/store.py` — `PredictionStore`**, the minimal writer:
   - `record(claim_id, event_id, predicted_prob=..., context_key=...)` —
     append-only; a recorded prediction is never edited or deleted
     (preregistration semantics at storage level).
   - `resolve(prediction_id, outcome)` — one verdict per prediction;
     re-resolution is rejected unless `overwrite=True`, so corrections are
     explicit rather than silent history rewrites (the exact shape of defect
     the gate-direction guard exists for).
   - Unknown outcome tokens raise: a typo'd verdict must not silently become
     "unresolved" and vanish from scoring.
   - Domain vocabulary normalised through `_norm_outcome` ("confirmed",
     "retracted", "won", "yes").
3. **Tests** (`tests/test_prediction_store.py`, 6 tests): migration up/idem-
   potent/down; the real round-trip record→resolve→`summarize()` scored by
   the real resolver; double-resolve rejected without correction flag then
   corrected with it; typo'd token raises leaving no row; required-field
   validation.

## Before / after

- Before: a domain-general claim had **zero** durable paths into lifecycle
  scoring — resolver read tables that no deployment schema creates, and no
  writer existed at all.
- After: migrate (automatic via the existing runner), then
  `store.record(...)` / `store.resolve(...)`, and the claim scores through
  `GenericPredictionResolver.Sqlite` with everything downstream (Brier,
  calibration, inheritance caps) working unchanged.
- Sports: untouched. Betting keeps paper_trades/clv_log; migration is purely
  additive. Lifecycle tests green: 76 passed across test_hypothesis,
  promotion_gates, b1_clv_gate, b4_inheritance, resolvers, prediction_store.

## Suite honesty

Full run: 2142 passed, 26 failed, 12 skipped — with two collection errors
from a pre-existing local env issue (xgboost needs libomp, absent on this
Mac). I verified the 26 failures are pre-existing: a baseline check with the
working tree stashed reproduced them identically (8/24 in the affected files,
including files owned by concurrent instances' in-flight work — w6 market
probability planning, redteam pipeline wiring). None of the failing test
files imports anything I touched. Note: my stash-based baseline check collided
with `data/artifacts/index.json` on pop; resolved cleanly and the stash list
was verified restored to its exact prior snapshot (3 entries, same SHAs).

## What I deliberately did NOT do

- Did not wire the generator to emit generic claims yet — hypothesis_generator
  is sports-shaped by design today, and pointing it at new domains without a
  live retrieval loop would add machinery nobody drives. The write path being
  real is the precondition; the first real non-sports question should pull the
  rest of it into existence (NEXT.md discipline: let reality prioritise).
- Did not touch gates, FWER logic, CAS, or auto_promote — read end to end;
  they are in good shape (direction-guarded, CAS-scoped, honest about
  insufficient data).
