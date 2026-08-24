# Stopping rules — when did marginal evidence stop changing the conclusion?

Date: 2026-08-23 · Branch: `build/derived-analysis-loop` (worktree
`~/callisto-wt/epistemics`) · Commit: 8f45d1e

## The problem

Callisto has no concept of "enough". The measured bottleneck is the MODEL
(>600s per call; process spawn <0.07%), so every avoided model call is
minutes saved. Retrieval rounds exist to feed that model; a round whose
evidence cannot change the conclusion is pure cost.

## Method (measurement FIRST, no constants invented)

JOB 1 — Instrumentation. `IterativeRetriever` gained an optional
`round_observer`: after every retrieval round it reports the CUMULATIVE
conclusion-relevant state:

    {qid, round, indep_keys, admitted[(source, sha256)], rejected_n}

This is complete: a leaf's sealed tier/confidence is computed from exactly
(best provenance class of admitted fetches, len(independent_keys), sandbox)
and its stance from the admitted bodies. Default `None` = pre-change
behaviour, byte-identical.

A round is CONCLUSION-MOVING iff that state changed vs the previous round.
Otherwise it is PURE COST: the downstream model call would receive an
identical evidence payload and cannot return anything new.

JOB 2 — Distribution over golden runs. Corpus:
`scripts/golden_corpus.py` — 27 cases = 5 hand-built retrieval shapes
(sufficient-first-round, all-irrelevant null, single-source, scholarly,
economic) + the 22-question retrodiction set mapped onto plannable sources,
all through the REAL retriever with fixture transport.

Results (`data/stopping_rules/round_distribution.json`, n=79 leaf-rounds):

| ordinal round | conclusion-moving | pure cost |
|---|---|---|
| 1 | 27 | 0 |
| 2 | 13 | 13 |
| 3 | 13 | 13 |

Pure cost: 26/79 rounds = **32.9%**. Every moving-after-round-1 case moved in
EVERY subsequent round (a new independent key landed each round); every
pure-cost tail stayed pure cost to the budget. There were no
recover-after-stasis cases: once a round changed nothing, nothing ever came.

Null split at stopping time (kept separate, per mandate):
14 answered, **12 honest_null** (sources reached, gate judged real
responses, nothing relevant), **1 retrieval_failure**
(FRED needs a key; sources 404'd). These are different claims and the data
keeps them distinct.

## JOB 3 — The stop rule, derived from the data

The distribution says the signal is not "confidence plateaued" but STATE
STASIS. So the rule (`tools/pipeline/stasis_stop.py`, wired opt-in as
`retriever.stasis_stop`):

> Stop when a finished round changed NEITHER the independent-key set NOR the
> admitted-body set.

Not a saturation threshold — no constant was tuned. It is a state-change
rule with one structural guard: it fires only when sufficiency has not
already been met (so "sufficient:" keeps its reason) and it never reads any
confidence number, so nothing can be raised.

Honest-null interaction (the mandated distinction): stasis changes only WHEN
fetching stops, not HOW a null is classified. `classify_null_kind()`
(tools/gaps) runs unchanged on the stopped trace; the trace carries both the
stasis stop-reason AND the gap verdict. "More evidence stopped helping"
(stasis on admitted evidence) remains distinguishable from "there is nothing
there" (honest_null) and from "we never looked properly"
(retrieval_failure). Test `test_stasis_identical_conclusion_fewer_rounds_on_
null` pins this: an all-irrelevant corpus stops earlier AND keeps
`honest_null`.

## Proof — conclusions byte-identical, fewer fetches

`scripts/proof_stasis_identical.py`, results in
`data/stopping_rules/stasis_proof.json`:

- 27/27 golden cases: final state (best class, indep keys, distinct shas)
  IDENTICAL with vs without the rule → identical sealed tier, confidence,
  stance inputs.
- Fetch attempts: 213 → 157 = **26.3% fewer source calls**, i.e. at least
  one full model round-trip avoided per null/stale-tail leaf.
- Full test suite before vs after: same 34 failures
  (`/tmp/baseline_failures.txt`; pre-existing, backtest_e2e /
  redteam_artifacts / redteam_confidence_laundering families), 0 new,
  11,183+ passing. New tests: `tests/test_stasis_stop.py` (3 passed).

## Honest caveats

- Golden runs use fixture transports: the distribution measures ROUND
  STRUCTURE (which is what the stop rule keys on), not live-source variance.
  A live validation pass should re-run `measure_round_distribution.py`
  behind the real transport before flipping the default on.
- The rule is opt-in per retriever instance today; production wiring
  (`engine._fetch_for_question`) still constructs without it. One line to
  enable once live-run confirms the offline shape.
- Duplicate-sha rounds (same body re-admitted) move nothing downstream by
  construction — the sealed number uses best-class + independent-key count;
  dedup shas are the stance payload. This equivalence is asserted in the
  proof script.
