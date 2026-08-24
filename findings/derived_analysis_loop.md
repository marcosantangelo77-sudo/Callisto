# Derived-Analysis Loop: structured extraction → expected relationship → deviation → QUESTION

Branch: `build/derived-analysis-loop` (commits through 54e9a81).
Baseline: 47 known failures; verified this branch's failure set is **identical to clean HEAD**
(34 failures in 5 suites — redteam_artifacts_store, backtest_e2e, confidence_laundering,
lifecycle_claim, prop_scanner — reproduced byte-for-byte on a detached worktree at HEAD;
plus 2 collection errors in ml_classifier/ml_drift that also pre-exist). New tests: 12, all pass.

## The gap closed

`tools/domains/finance/statements.py` assembles three-statement financials.
The hypothesis generator lives elsewhere and knows nothing about them.
Extracted structure died as fact: gross margin was computed, filed under
`derived=True`, and never became a line of inquiry.

## The mechanism (domain-generic)

`tools/derived_analysis.py`:

1. A `Relationship` computes one observation per period from an extracted
   series (`{label: {period: value}}`) — any domain can supply these.
2. The **normal range comes from the entity's own history**, not a textbook:
   median ± 3·(1.4826·MAD) of that entity's own baseline periods
   (`MIN_PERIODS_FOR_RANGE = 3`; the focus period is excluded from its own
   baseline). A company whose accruals share has been 0.37–0.38 for a decade
   gets a tight band; a volatile company gets a wide one. This is what makes
   the flag *defensible* — it says "this entity stopped behaving like itself."
3. A deviation becomes an `Anomaly`, which carries evidence (observed value,
   normal range, basis periods, magnitude in robust sigmas) and renders ONLY
   as a question string via `.question()`.

### Hard rules, enforced structurally

- **An anomaly is a QUESTION, never a finding**: `Anomaly` has no confidence
  field (tested: no field name contains "conf"), exposes only `.question()`
  and `.evidence()`, and nothing in the code path touches any confidence
  store. It cannot seed a conclusion because it has no conclusion-shaped
  output.
- **No confidence score is raised anywhere**: emission goes through
  `TaskQueue.submit_task` exactly like a human-submitted query. The AGP
  pipeline's confidence/provenance machinery applies to the RESEARCH it
  triggers, not to the question.
- **Bounded**: `MAX_QUESTIONS_PER_EXTRACTION = 5`. One extraction can raise at
  most 5 questions regardless of how many relationships × periods deviate.
  Selection is deterministic (largest robust-sigma deviation first, ties by
  key/period) and the drop count is reported in every payload
  (`dropped_over_bound`). No feedback loop exists from emitted questions back
  into extraction, so the loop cannot compound.

## Finance instantiation

`tools/domains/finance/derived_analysis.py` — five relationships, chosen not
as "more ratios" but because each is computable from already-assembled lines
and each has abnormal behaviour that classically warrants research:

| key | inputs | what its deviation asks about |
|---|---|---|
| `accruals_share` | net_income / cfo | earnings quality — NI outrunning cash |
| `cash_conversion` | free_cash_flow / net_income | is profit turning into cash |
| `gross_margin` | gross_profit / revenue | pricing/cost-structure shift |
| `capex_intensity` | capex / revenue | investment-cycle break |
| `current_ratio` | current_assets / current_liabilities | liquidity regime change |

Wiring: new `edgar_anomalies(ticker, n_periods, emit)` tool on the finance
plugin. Default reports questions + evidence; `emit=true` submits each
selected question to the task queue for ordinary research.

## Worked example (real assembled data, TestCo fixture)

From `assemble_statements()` over synthetic-but-messy companyfacts (retired
tags, restatement, non-USD units — the same fixture the B6 suite uses), with
a FY2020–FY2021 history added and FY2023 net income restated upward:

    ASSEMBLED MATRIX ($B):
      revenue         FY2020 60.0  FY2021 70.0  FY2022 78.0  FY2023 100.0
      cfo             FY2020 20.0  FY2021 24.0  FY2022 28.0  FY2023 33.0
      net_income      FY2020 8.0   FY2021 9.0   FY2022 10.5  FY2023 90.0   ← injected break
      gross_profit    FY2020 30.0  FY2021 35.0  FY2022 38.0  FY2023 55.0

Loop output (focus period FY2023):

    ANOMALIES DETECTED: 2; EMITTED (bound=5): 2; DROPPED: 0

    {"relationship": "accruals_share",
     "expectation": "net income vs operating cash flow",
     "entity": "TESTCO INC", "period": "FY2023",
     "observed": 2.727273,
     "normal_range": [0.374999, 0.375001],
     "range_basis_periods": ["FY2020", "FY2021", "FY2022"],
     "magnitude_in_robust_sigmas": 6272727.273, "unit": "ratio"}

    QUESTION: Investigate: why is TESTCO INC's accruals_share (net income vs
    operating cash flow ...) 2.727 ratio in FY2023, outside the range
    [0.375, 0.375] its own history (FY2020, FY2021, FY2022) implies?
    Deviation ~6272727.3 robust sigmas from the historical median. Evidence: {...}

A second question fired on gross_margin (0.55 vs own-history ~0.50). Both are
questions with attached evidence; neither asserts an answer, carries
confidence, or seeds a finding. Note the honest behaviour on thin history:
the zero-dispersion epsilon band produces huge sigma numbers on this
synthetic series — real filers with 4+ periods of noise get sane magnitudes,
and the magnitude number is diagnostic either way.

Also demonstrated: silence where the system should be silent. With the
unmodified fixture, current_ratio has balance-sheet instants at only one date
— fewer than MIN_PERIODS_FOR_RANGE baseline periods — so it raises nothing.

## Generalisation past finance

The engine (`tools/derived_analysis.py`) imports nothing financial. A
non-finance instantiation needs only Relationship objects over extracted
series:

- **Security/vuln scanning**: extraction = parsed auth logs per service per
  day. Relationships: failed-login share of attempts, off-hours request
  share, new-endpoint traffic share. Normal range from that service's own
  trailing window; a deviation emits "Investigate: why did svc-auth's
  failed_login_share hit 0.31 in week 34 vs [0.02, 0.06] its own 12-week
  norm?" — a question routed to the same research pipeline, not an alert
  verdict.
- **Sports (Callisto's original domain)**: extraction = per-player game logs;
  relationship = usage rate / minutes; a player suddenly far outside their
  own 20-game band becomes a researchable question about role change or
  injury rather than a concluded bet.
- **Any XBRL-like structured source** (weather stations, CI metrics): same
  shape — extract, expect, deviate, ask.

The finance module is therefore an instantiation, not the feature. Adding a
new domain is: write N Relationships + a series adapter + optionally an emit
hook; bounds, honesty rules, and question-only semantics come for free.

## Files

- `tools/derived_analysis.py` — generic engine (Relationship, Anomaly,
  detect_anomalies, select_for_emission, emit_questions)
- `tools/domains/finance/derived_analysis.py` — five finance relationships
- `tools/domains/finance/plugin.py` — `edgar_anomalies` tool + dispatch
- `tests/test_derived_analysis_loop.py` — 12 tests covering the hard rules
