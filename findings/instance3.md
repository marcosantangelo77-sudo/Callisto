# Instance 3 findings — the promotion gate (tools/hypothesis.py, tools/backtest.py, tools/hypothesis_generator.py, tools/ml_backtest.py)

Session opened 2026-08-22 on branch audit/tier2-gate.
Method: START_HERE.md brief; AUDIT_MANDATE §2 protocol; ROADMAP.md treated as unverified prior claims, re-derived independently where load-bearing.
Peer status at session open: instance 1 published header only; tier{0-money,3-epistemics} do not exist yet.
Note on scope: characterization tests are NEW uniquely-named files under tests/ prefixed `test_tier2_gate_*` (no existing file outside my four is edited).

---

## WORK UNIT 1 — independent verification of the Šidák / reachability arithmetic

Method: pure-stdlib reimplementation (no scipy/numpy on this machine, no imports from
the codebase) of the one-sided binomial test (exact ≤30, normal+cc >30 — mirroring
`binomial_pvalue` at hypothesis.py:343), Šidák correction, and binomial power
analysis under a true edge. Scripts retained in session temp dir; all numbers below
reproducible from the stated formulas.

## [VERIFIED] ROADMAP §3.2 headline arithmetic — CONFIRMED, every number reproduced
Blast radius: LOUD (this is the audit's load-bearing claim and it holds)
Evidence: independent stdlib computation. α_per = 1−(1−0.25)^(1/3192) = **9.012e-05**
(ROADMAP: 9.0e-05). True +3pt edge over −110 breakeven (null 0.5238): median-clearing
n ≈ **3,887** (claimed ~3900); +1pt: **34,987** (claimed ~35000). Perfect record clears
at n=14 (fair null) / n=15 (vig null) (claimed "n≈15"). Old Catch-22:
P(p≤0.15 | true +3pt, n=30) = **0.144** (claimed ~0.144).
Falsifier: rerun the closed-form formulas with different inputs; any spreadsheet can check row 1.
For: unowned (context for the gate rebuild)

## [VERIFIED] hypothesis.py:1172 — the 9e-05 threshold controls FWER at 25%, not 5%
Blast radius: SILENT (conceptual error that will misdirect any future tuning debate)
Evidence: `sidak = 1.0 - (1.0 - max_p)**(1.0/fwer_n)` where max_p is the ADAPTIVE
p-gate (base 0.25 for backtesting→paper_trading), not a significance level α.
Šidák's contract is per-test α given a family-wise budget; here the budget IS the
lax small-sample gate. With the classic α_family=0.05 the same N gives 1.607e-05.
The comment block (:1122-1128) says "α_family = 0.05" — the code does something else.
Consequence: the effective family-wise false-positive budget across all hypotheses
per window is 25%, which is not a rigor statement anyone signed up to.
Falsifier: show max_p fed to line 1172 is ever 0.05 in the backtest→paper path (it is 0.20–0.30 by construction, :282-289).
For: Instance 3 (gate rebuild design)

## [VERIFIED] hypothesis.py:87 — lifetime denominator claim is STALE; default is now a 365-day rolling window
Blast radius: LOUD for the audit narrative (ratchet is bounded per-year by default), SILENT for docs
Evidence: `FWER_LOOKBACK_DAYS_RAW = os.getenv("CALLISTO_FWER_LOOKBACK_DAYS", "365")`.
Both comment blocks still describe lifetime semantics: :82-86 ("'inf' counts every
hypothesis ever tested... With 4500+ lifetime hypotheses") and :1122-1128 ("the
**lifetime** pool"). Q1 doc/code disagreement. The ratchet survives inside the window:
any 12-month span containing ~3,200 backtested hypotheses reproduces α≈9e-05.
Whether the workstation env pins inf is UNKNOWABLE from this sandbox — query required.
Falsifier: `SELECT COUNT(DISTINCT hypothesis_id) FROM backtest_runs WHERE completed_at > date('now','-365 days')` at the workstation ≠ lifetime count, and env shows CALLISTO_FWER_LOOKBACK_DAYS unset.
For: Instance 3 (docs fix is mine); workstation env check unowned

## [VERIFIED] hypothesis.py:1415 — the Catch-22 auto-reject was never removed; it moved and is now an inline literal
Blast radius: LOUD (primary reachability killer)
Evidence: `should_reject` includes `(p > 0.15 and n >= 30)` — documented in-comment at
:1405-1407 as "New: p>0.15 with 30+ signals". The constants were raised
(AUTO_REJECT_P=0.50, :168) but the old rule was re-added literally. For a TRUE +3pt
edge: P(killed at first gate evaluation with n≥30) = 1−0.144 = **85.6%**, while the
Šidák p-gate it must eventually clear needs n≈3,900. Between n=30 and n≈3,100
(where P(p≤0.15) reaches 99%), EVERY gate run re-applies this rule — survival to
promotion ≈ 0. Zero promotions in 3,192 trials remains the EXPECTED output.
Falsifier: produce one promoted-to-live hypothesis with <3,900 resolved signals under current code without legacy grandfathering.
For: Instance 3 (design answer below)

## [VERIFIED] hypothesis.py:1422-1429 — additional kill layer ROADMAP missed: hit-rate<45% at n≥12
Blast radius: LOUD
Evidence: `n >= 12 and hit_rate < 0.45 → should_reject`. Under a TRUE +3pt edge
(win prob 0.554): P(Bin(12,0.554) ≤ 5) = **25.2%** killed at the n=12 checkpoint alone.
Joint with the :1415 rule, a true winner has ≈ **10.8%** chance of surviving early
attrition — before beginning its ~3,900-signal climb against repeated evaluation.
Falsifier: simulation with seeded RNG showing higher joint survival.
For: Instance 3

## [VERIFIED] hypothesis.py:1354,1388,1457,1510 — Brier/IC waivers use raw p≤0.15 while the promotion gate demands Šidák-corrected 9e-05
Blast radius: ARMING (dormant today because nothing passes the p-gate anyway; goes live the moment the gate is repaired or N shrinks)
Evidence: waiver predicate `(p <= 0.15 and hit_rate > 0.55) or (hit_rate > 0.70 and n >= 10)`
treats p≤0.15 as "demonstrated real alpha"; the same function's p-gate requires
p ≤ 9.01e-05 (N=3192). Two incompatible evidentiary standards in one function,
~1,600× apart. Also extends ROADMAP C6: the waiver is not just Brier — it also
disables IC gating AND IC/Brier zombie rejection via the same predicate.
Falsifier: a gate evaluation log where a waived-Brier hypothesis passes with corrected-p ≤ 9e-05 (impossible today; trivially possible post-repair unless waivers are re-scoped).
For: Instance 3 (rebuild must scope waivers to the corrected standard)

## [INFERRED] Unit inconsistency: gate thresholds count pushes, the p-value does not
Blast radius: SILENT (small)
Evidence: `sample_size = resolved` (wins+losses+pushes, :758/:877) feeds min_signals,
auto-reject n≥30/15/12, and adaptive tiers; the binomial test itself uses decided =
wins+losses (:806). A hypothesis with many pushes ages into auto-reject territory
faster than its evidence base grows.
Falsifier: construct a report with ≥30 resolved, ≥6 pushes, p≤0.15 → no rejection though decided<30; mirror case rejects.
For: Instance 3

Nit: ROADMAP's "z ≈ 3.78" for α=9.0e-05 — exact one-sided z is 3.745. Immaterial.

### Design question (brief): what SHOULD the multiple-comparison correction be scoped to?
Deferred to the dedicated section at end of file once backtest.py/ml_backtest.py
characterization is in place — recorded here so the brief item is not lost.

---

---

# Continuation — Claude (Opus 5), 2026-08-22
Instance 3 was cut off mid-brief by an OpenRouter daily rate limit. Picking up
from its findings; all seven were re-verified against the code before building
on them.

## [VERIFIED] Re-verification of Instance 3's load-bearing claims — all three hold
Blast radius: n/a (process)
Evidence: read directly at hypothesis.py:87 (`os.getenv("CALLISTO_FWER_LOOKBACK_DAYS", "365")`),
:1172 (`sidak = 1.0 - (1.0 - max_p) ** (1.0 / fwer_n)`), :1415 (`or (p > 0.15 and n >= 30)`),
:1422-1429 (hit_rate<0.45 at n>=12). Every claim reproduced.
Falsifier: those line numbers showing different code.

## [VERIFIED] hypothesis.py:1176 — the audit log LABELS a 365-day window as "lifetime"
Blast radius: SILENT (corrupts the audit trail this system relies on for trust)
Evidence: `denom_tag = ... f"lifetime (lookback_days={FWER_LOOKBACK_DAYS})"` emits
the literal string `lifetime (lookback_days=365.0)` — self-contradictory. Anyone
reading gate logs to diagnose the zero-promotion problem is told the denominator is
lifetime when it is a rolling year. This is how the ROADMAP's lifetime claim survived.
Falsifier: a gate log line reading "lifetime" while FWER_LOOKBACK_DAYS is inf.
For: gate rebuild

## [VERIFIED] The killer rule is unreachable by configuration — 12 tunable constants, 1 hardcoded literal
Blast radius: LOUD (explains why a prior fix attempt failed)
Evidence: hypothesis.py:168-183 defines twelve AUTO_REJECT_* constants. The rule that
actually kills true edges — `p > 0.15 and n >= 30` — is an inline literal at :1415.
AUTO_REJECT_P was raised to 0.50 (":168, Reject only when signal data actively
disproves thesis"), evidently an attempt to loosen the gate. It could not have worked:
the literal is unaffected by every constant in that block. An operator loosening the
gate by config sees no change and concludes the gate is not the problem.
Falsifier: find a config path that alters the :1415 predicate.
For: gate rebuild

## [VERIFIED] The correction method is NOT the binding constraint — effect size is. My own FDR proposal fails.
Blast radius: LOUD (redirects the entire gate-rebuild effort)
Method: pure-stdlib power analysis, signals needed for a one-sided binomial test to
clear each threshold at ~50% power against the -110 breakeven null (0.5238).

      edge    win% |   p=0.05  BH q=.10  Sidak 25%  Sidak 5%
    + 1pt   0.534 |    6,749    20,652     34,988    43,120
    + 3pt   0.554 |      750     2,295      3,888     4,792
    + 5pt   0.574 |      270       827      1,400     1,725
    + 8pt   0.604 |      106       323        547       674
    +12pt   0.644 |       47       144        243       300

I hypothesised that replacing FWER (Šidák over cumulative N) with FDR
(Benjamini-Hochberg over the evaluation batch) would restore reachability. It does
not. BH cuts the requirement for a +3pt edge from 3,888 to 2,295 signals — a real 41%
improvement, but still unreachable. Even an UNCORRECTED p=0.05 needs 750 resolved
signals for a +3pt edge. A Monte-Carlo of the full batch (M=3192, 2% true edges,
seed 20260822) promoted 0-1 hypotheses under BOTH methods at every n from 30 to 1000.

The conclusion the data forces: **per-hypothesis significance testing of small edges
is structurally mismatched to the data this system will ever have.** 3,192 hypotheses
× 750 signals each = 2.4M resolved bets for uncorrected significance alone. Changing
the correction rearranges deck chairs.
Falsifier: a power calculation showing a realistic sports edge clearing any of these
thresholds at signal counts this system actually accumulates (median resolved signals
per hypothesis — a workstation query — would settle it immediately).
For: gate rebuild

## DESIGN ANSWER — what the correction should be scoped to (the deferred brief item)
The question as posed ("per family, per window, per sport?") assumes the answer is a
scoping choice. The power analysis says it is not. Three changes, in order of impact:

1. **Pool, don't isolate.** Test hypothesis FAMILIES, not hypotheses. Fifty related
   hypotheses (same sport × market type) at 60 signals each is 3,000 observations;
   a hierarchical model estimates the family effect plus per-hypothesis deviations
   and gets power at realistic n. Isolated testing throws away exactly the structure
   that makes the problem tractable. This is where the multiple-comparison question
   actually belongs — correct across FAMILIES (dozens), not hypotheses (thousands).

2. **Invert the gate order.** Paper trading is free out-of-sample replication. The
   current design puts the strictest statistical test BEFORE the cheap replication
   step, which is backwards. backtesting→paper_trading should be a generous screen
   (accept a high false-discovery rate; paper trading kills false positives at zero
   cost); paper_trading→live should carry the strict test. Today a hypothesis must
   pass 9e-05 to earn a free simulation.

3. **Gate on CLV, not win rate.** Closing-line value has far lower variance than
   realised outcomes — it measures whether you beat the market's final price rather
   than whether a coin landed right. Positive CLV over 200 bets is stronger evidence
   than 55% hit rate over 200 bets, at a fraction of the sample. This raises the
   priority of the known CLV unit bug (ROADMAP §3.3) from "fix the live gate" to
   "fix the statistic the whole rebuild should depend on."

Note the interaction with the :1415 literal: none of this matters until that rule is
removed. A true +3pt edge has ~10.8% joint survival past early attrition (Instance 3),
so 89% of real edges die before any correction method is consulted.
