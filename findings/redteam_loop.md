# RED TEAM — SELF-DECEPTION IN THE AUTONOMOUS LOOP

Scope: `tools/autonomous.py`, `tools/self_repair.py`, `tools/loop_quality.py`,
`tools/hermes_memory.py`, `orchestrator.py`, `agp/adversary.py`, plus the
retrodiction scoring path it feeds. Tests: `tests/test_redteam_loop_*.py`
(4 files). Method: incentives first — what is each component REWARDED for,
and can that reward be collected without the underlying goal being met?

The framing that organised everything: **the historical failure was not a
bug, it was an incentive equilibrium.** A routine punished for reporting
"nothing fixed" and never checked on whether fixes worked will always
drift toward cosmetic repairs stamped as success. Every finding below is a
place where that equilibrium still exists.

---

## H1 — What each phase's metric actually pays for

| Phase | Metric | Collected without the goal? |
|---|---|---|
| Self-repair | `fixed` count in cycle summary | YES — see V1–V3 |
| Progress watchdog | Δsignals + Δpromotions / 10 cycles | YES — one noise signal resets spin detection (H5) |
| Research loop termination | confidence plateau (info-gain) | YES — stagnation without learning (H2) |
| Compaction | "contradicting never dropped" guarantee | YES — only if labelled correctly on arrival (H4) |
| Hermes learnings | recency-ranked context injection | YES — restating a prior outranks discovery (V5) |
| Adversary track record | precision_of_attack | YES — silence and easy-target farming both flatter (H3) |
| Retrodiction | mean Brier over answered questions | YES — cherry-picking is invisible (H6) |

## Findings with tests

### V1 — Disabling is reported as fixing (`self_repair._fix_scraper`)
A broken scraper's "repair" is disabling it; the result reports
`fixed=True` whenever any other scraper remains active. The truthfulness
of the flag depends on fleet size, not repair. `_record_to_hermes` then
prefixes the value "FIXED" at write time from the actor's own flag — there
is no verification hook anywhere between `result["fixed"]` and the label
that enters prompt context. Test:
`test_redteam_loop_selfdeception.py::test_disabling_a_broken_scraper_reports_fixed`,
`::test_fixed_label_is_self_reported_not_verified`.

**Fix direction:** `fixed=True` must require a post-condition probe
(re-run the detector, confirm it no longer fires), not the actor's claim.

### V2 — The rate limiter is an unguarded gate (`_fix_claude`, Heartbeat)
GATE_WRITE_PATTERNS covers promotion gates. The Claude budget/rate-limit
is also a gate — a spend gate — and self-repair opens it autonomously
(`cc._call_count = 0`) and records a success. This is exactly the shape of
the original sin: a maintenance routine widening a constraint because the
detector told it to. Test: `...test_claude_counter_reset_is_an_unguarded_budget_gate_write`.

**Fix direction:** either add budget resets to the gate policy (env opt-in
like `CALLISTO_ALLOW_PREMATURE_REQUEUE`) or accept them explicitly as
policy in GATE_POLICY docs. Today they are invisible to the policy.

### V3 — Dead knobs still stamp success (`_fix_finding_low_sample`)
Repo-wide grep proves `minimum_events` (model_config key) has NO reader
outside self_repair — the same dead-knob shape ROADMAP §3.1 documented.
It reports `fixed=True` ("Set minimum_events=30 on N hypotheses") while
touching nothing any evaluator reads. Test pins this statically so the
finding retires itself if a consumer ever appears.

### V5 — Restatement outranks discovery (`hermes_memory` upsert)
The upsert bumps `occurrences` and refreshes `learned_at`;
`get_actionable_learnings` orders by `learned_at DESC`. A loop that
re-states its old beliefs occupies the top context slots ahead of genuinely
new discoveries. The MAX-confidence ratchet is gone, but the RECENCY
ratchet rewards repetition. Test:
`...test_restated_prior_displaces_new_learning_via_recency`.

**Fix direction:** rank by (first_learned_at desc, occurrences asc), or
penalise occurrence count in ordering; treat re-recording identical values
as an observation, not news.

## H2 — The information-gain terminator is satisfiable by producing nothing

`InformationGainTerminator` reads ONE signal: marginal Δconfidence. Three
degenerate strategies all satisfy it:

1. **Null run**: broken retrieval → model emits the same default every
   iteration → stop at min+needed−1 with reason "additional evidence is no
   longer materially moving the estimate". Nothing moved because nothing
   arrived; the reason text asserts convergence that never happened.
2. **Wobbling run**: ±0.021 perturbation per iteration defeats the
   terminator entirely and burns max budget looking diligent.
3. Both produce byte-similar StopDecisions to an honest converged run;
   `StopDecision` carries no evidence counts, so downstream cannot tell
   them apart. `LoopCalibrationTrace.summary()` flags rising-confidence-
   with-no-dissent but has NO flag for zero-evidence runs at all.

Tests: `test_redteam_loop_terminators.py` (first four).

**Fix direction:** terminator decisions must carry the calibration trace
counts (evidence_total delta since last iteration); refuse
`info_gain_stalled` when evidence_gain == 0 across the stagnant window —
that is a retrieval failure code, not a convergence code.

## H4 — Disconfirming evidence can vanish before compaction

`compact_state` guarantees contradicting items survive — but only items
that ARRIVE spelled `"contradicting"`. Any other string silently becomes
`neutral` (budget 4, tier-ranked). Six genuine dissents mislabelled
("refutes", "against", uppercase-with-space…) are dropped under budget
while the guarantee stays technically true. The bias is real but the
enforcement point is downstream of the trust boundary: whoever classifies
stance controls whether the bias can act. Tests: parametrised mislabel
cases.

**Fix direction:** unknown stance strings should be treated as
contradicting (fail toward preserving dissent), not neutral; stance
assignment should carry provenance and be audited against the source text.

## H5 — No progress dressed as progress

- `evaluate_progress_window`: ANY new signal resets the no-progress streak.
  One threshold-noise blip per window keeps the loop permanently
  "productive"; promotions can stay zero for months with no diagnosis ever
  firing. And the retroactive `signal_generated` rewrite (documented
  contamination) registers as a productivity surge under this exact metric.
- Resolution recorded for a claim that never resolved: the promotion-side
  counters never verify that resolutions correspond to real ground truth
  arrival; nothing in the loop distinguishes "resolved" from
  "resolution pipeline asserted".

Tests: `test_one_trivial_signal_resets_the_spin_detector`,
`test_signal_count_is_volume_not_value`.

**Fix direction:** progress needs a quality-weighted component — e.g.
promotions-only streaks, or signals weighted by later survival through
evaluation — plus an absolute promotions-stall alarm independent of signal
volume.

## H3 — The adversary's calibration record is gameable

Five vectors, all demonstrated:

1. **Silence is free**: never objecting → n_scored=0 → verdict
   "insufficient_data", precision None. No mechanism punishes a critic
   that objects to nothing. The attack prompt even invites it ("return
   zero objections rather than manufacturing weak ones") — correct
   epistemics, unpriced incentive.
2. **Easy-target farming**: object only to weak claims → precision 1.0,
   ranked above an honest critic that attacks strong claims too (0.5,
   and 'too_harsh' past 6 survivors out of 10).
3. **One-bit scoring**: `record_resolution` scores EVERY objection on a
   claim RIGHT/WRONG from the single bit `claim_was_correct`. Prescient
   objection and pure noise get identical outcomes; objection quality is
   structurally unmeasurable.
4. **String-matched statuses**: `record_overrule` matches by exact
   objection TEXT — duplicate texts flip together, and one decision can
   launder another's status. `n_sustained` is controllable by repetition.
5. **Invisible outages**: backend failure returns a BLOCKING objection
   BEFORE `record_objection` — a persistently-failing adversary is
   indistinguishable from a perfectly silent one. The track record cannot
   see its own downtime.

Also: `apply_verdict` accepts any list it is handed; nothing binds "the
model produced these objections" to "these penalties were applied".

**Fix direction:** score the adversary on COVERAGE (objection rate on
claims that fail with no objection raised = misses, the false-negative
side precision ignores); attribute objections to (claim_id, content-hash)
not text; record outage objections in the ledger flagged UNSCOREABLE.

## H6 — Retrodiction can be scored well without researching well

- `score_brier` averages the prediction∩question intersection only.
  Skipping hard questions improves mean Brier and leaves no trace in the
  returned float — coverage is not part of the contract. A config that
  answers only sure-things wins the A/B harness.
- Question objects carry `answer_binary` in-process next to
  `prompt_for_researcher()`; one harness mistake passes truth to the model
  and Brier=0.0 is indistinguishable from brilliance.
- Skewed question sets reward confident constants (0.9-always beats honest
  0.8 on an 80%-true set); verdicts like "strongly better than chance"
  would bless base-rate riding with no difficulty/base-rate control.
- `resolved_claim_record` feeds the INHERITANCE RULE on direction alone:
  p=0.5000001 counts as a full "hit" half the time — lucky coins raise
  parent ceilings.

**Fix direction:** report coverage (n_attempted/n_total) alongside mean
Brier and make the A/B verdict conditional on a minimum coverage; strip
answer fields into a researcher-invisible wrapper type; z-score per-question
difficulty or stratify slices before comparing configs; weight inheritance
hits by predicted-probability extremity.

## H7 — Where the NEXT doom loop forms

The historical loop was: bad gates → zero promotions → detector fires →
maintenance weakens gate → fake success learning → detector quiet → repeat.
Its fuel was **a detector whose silence could be bought**. Candidate
successors, same topology:

1. **The spin-detector bribe (live today).** Feedback path: noise signal →
   `progressing=True` → streak reset → diagnosis suppressed → underlying
   breakage persists undiagnosed indefinitely. Metric: Δsignals. Pressure:
   anything that manufactures a signal (threshold drift, retro rewrite)
   silences the only alarm. This is the original loop wearing a hat: the
   system learned that *any* signal output quiets the watchdog, and
   signal volume is the easiest number to move.
2. **The adversary-avoidance loop (latent).** Once per-model×domain
   calibration feeds empirical routing, models are selected partly on
   objection records. A router that deprioritises critics with low
   precision selects for silence and easy targets (H3 vectors 1–2) →
   weaker criticism → higher sealed confidence → more claims resolve wrong
   → ... The metric (precision_of_attack) optimises against the goal
   (falsification) unless coverage is scored.
3. **The retrodiction selection loop (arriving soon).** Routing scores
   come from `write_routing_scores`, which writes only non-null scored
   results. Configs that null out hard questions avoid losses in the
   store → routing favours them → the measured population skews easy →
   scores inflate → routing trusts them more. Absence-as-honesty is right
   for the audit trail and wrong for a ranking input.
4. **The restatement loop (live today).** Recency-ordered learnings reward
   re-recording priors (V5) → context fills with restatements → the model
   sees "well-established" old ideas → re-states them again → occurrences
   climb → looks increasingly confirmed. Confidence-by-repetition through
   the memory layer's front door.

Common structure: **every one is a place where a component can improve its
own measured numbers faster than it can improve reality.** The defence is
never another guard band; it is making each metric cost something to move
— post-condition verification for `fixed`, coverage for Brier, misses for
precision, evidence-count for stopping.

## Priority

| # | Finding | Severity | Why |
|---|---|---|---|
| 1 | H7.2/H3 — adversary precision feeds routing unscored-for-coverage | HIGH | builds a silence-selecting critic permanently |
| 2 | V5 — recency ratchet rewards restatement | HIGH | live today; corrupts every prompt |
| 3 | H2 — terminator stops on empty runs | MED-HIGH | seals look converged when retrieval broke |
| 4 | V1/V3 — fixed-flag theater | MED | same equilibrium as the original sin |
| 5 | H5 — one signal buys silence | MED | suppresses the loop's only honest alarm |
| 6 | H6 — retrodiction coverage gap | MED | matters the day routing goes live |
| 7 | H4 — compaction label laundering | MED | bias unreachable via mislabelling |
| 8 | V2 — rate-limit reset unguarded | LOW-MED | spend gate, not epistemic gate |

## What held up

Credit where due, verified by attack: the gate policy's refusals
(`promotion_thresholds_strict`, `edge_ceiling`) genuinely refuse regardless
of caller; the hermes admission policy really does cap unsealed claims to
INFERRED and replace (not ratchet) confidence; `clamp_with_ensemble` floors
rather than rounds; the cutoff enforcer excludes rather than assumes. The
remaining holes are almost all **metric-shape holes, not enforcement
holes** — which is the point of this red team: the last war was lost in
metric shapes too.
