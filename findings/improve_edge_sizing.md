# EDGE QUANTIFICATION AND SIZING — improvement pass (improve/rotating-0823-213957)

**Area chosen: edge quantification and sizing** — tools/edge.py, tools/kelly.py,
tools/sizing.py, and their kalshi-domain consumers. Not covered by any prior
improve run (CLI ×2, retrieval, synthesis, artifacts/sandbox, retrodiction,
memory-wiki, source-health, information-gain all taken). This is NEXT.md §2:
the bridge from a sealed conclusion to a position. Every dollar number the
system ever produces flows through these ~1,300 lines, so an error here is
denominated in money, not in confidence points.

## What was wrong — measured

### 1. CRITICAL: `assess_edge` ignored `quote.kind` when pricing the payout
(family 4 — a label decides a trust outcome; family 7 — tests only fed one input class)

`assess_edge` computed market probability via `quote.implied_probability()`
(honouring kind) but computed EV/Kelly via `_to_decimal(quote.price)`, which
re-runs AUTO detection and ignores kind entirely. For the documented Kalshi-style
case `MarketQuote(price=47, counter_price=54, kind="contract_cents")` with
calibrated_prob=0.60:

    ev_per_unit = 27.2     (true: 0.277 — off by ~98x)
    kelly_full  = 0.25 cap (true uncapped: 0.245)

The contract price of $0.47 was read as decimal odds 47.0. Any consumer acting
on that assessment would size a position ~100x too large. Kalshi's adapter
escapes today only because it emits kind="probability", where auto-detection
coincides; the moment anyone passes cents — the format edge.py's own docstring
advertises — the numbers are garbage. No test caught it because every test in
test_build_r5_edge.py feeds American odds.

**Fix:** `_quote_decimal()` routes non-auto kinds through the same path fair
probability uses; both halves of an assessment can no longer disagree about
what the price means. Property sweep pins it: the same market price in any of
four encodings must produce byte-identical EV/Kelly (500 random cases +
reference case). Dead `from tools.kelly import kelly_full` import removed.

### 2. Family-2 duplication: tier boundaries copied into the money path
`kelly._confidence_tier_from_score` hardcoded its own copy of the AGP tier
boundaries (0.90/0.75/0.55/0.30) that agp/thresholds.py owns. A boundary change
in thresholds.py would have left money sizing silently disagreeing with the
confidence system — the exact "floor_conf at six sites" shape from PATTERNS.md.
Now sourced from thresholds.py; a boundary-agreement test guards it.

### 3. Family-6: quantisation RAISED stake fractions
50,000-case sweep over (edge, odds): `kelly_full`'s `round(..., 6)` raised the
stake fraction in **18,622** cases (max drift 5e-7/bet — small per bet, but the
direction is the whole point: an automated actor increasing a stake).
`kelly_fractional` compounded it (`round(0.238891*0.25,6)=0.059723 > 0.05972275`).
Both now floor at 6dp, matching the agp.thresholds.floor_conf direction rule.
Property tests pin: quantised value ≤ true value, always.

## Verified clean (hunted, found nothing — recorded to stop re-hunting)

- **Devig seam**: probe confirmed `fair_probability` converts correctly for all
  four kinds (my initial suspicion was wrong — the [1/raw] inversion is correct
  for decimal odds and identity for probabilities).
- **CLV direction**: 20,000-pair sweep found zero upward-rounding or
  inconsistency violations in clv_points/clv_basis_points.
- **Empty-input behaviour (family 3)**: absent counter-quote → flagged
  `devigged=False` with a phantom-vig note (fails open honestly); empty odds
  list → error dict, not silence; calibrated_prob outside (0,1) → ValueError;
  bankroll=0 → zero stake, no crash.
- **fermi.py**: validate() runs inside propagate(); empty factor list raises.

## NEW family candidate — "the verifier nobody wired"

tools/reference_class.py (classify_claim, reference_class_first,
record_outcome, empirical_base_rate — NEXT.md's third cheap win, "largest
single accuracy gain in the forecasting literature") has **zero production
callers**. The pipeline decomposer never consults it; only its own tests do.
Same shape for fermi.propagate: fully built, Monte-Carlo verified, workbook
emission included, and nothing in the pipeline can reach it. And EdgeAssessment
itself reaches production only through the Kalshi session tool — the lifecycle
stage NEXT.md §2 describes (sealed conclusion → edge → position) does not
exist yet; nothing persists an assessment against a claim_id. These are not
bugs — the code works — but they are capability built ahead of wiring, which
in this codebase has historically meant dead within months. Recommend either
wiring reference_class_first into the decomposition prompt context (one call)
or moving both to attic/ with restore notes until the lifecycle stage lands.

## Verification

- New tests: tests/test_edge_kind_pricing.py (kind-consistency property),
  tests/test_money_error_direction.py (direction + boundary guards).
- Full targeted suites green: 139 passed across edge/kelly/sizing/fermi/
  reference-class/kalshi.
- Whole suite (excl. xgboost collection errors, pre-existing): 11,215 passed,
  34 failed — all 34 reproduced on a clean origin/master worktree checkout
  (verified via git worktree, NOT stash), including backtest_e2e (known
  pre-existing) and the redteam/artifacts sets. My commits introduce zero
  regressions.

## Commits (pushed to improve/rotating-0823-213957)

- 7df5f01 edge: assess_edge honours quote.kind when pricing the payout
- d3ed8da kelly: tier boundaries sourced from agp/thresholds
- 6bbbb08 kelly: quantise stake fractions DOWNWARD, never round (family 6)
