# REDTEAM — money path (devig / edge / Kelly / CLV)

Date: 2026-08-23 · Instance: rotating red team · READ-ONLY on the money path
(no execution path armed; tests + findings only).

## Surface and why

**Surface: the money path.** Explicitly named unattacked ground: twelve prior
surfaces were attacked, four of them resume/checkpoint variants clustering
where the rotation last found blood. The arithmetic that turns a sealed
conclusion into a stake had never been swept.

**Method: property-based sweep over the full parameter space**, plus boundary
adversarial inputs. Chosen because PATTERNS.md ranks it the most productive
method used here (the 8,956-case confidence sweep found 1,385 violations) and
no prior pass applied it to this surface — the existing suites verify each
formula's docstring examples by hand (`[1.909,1.909]→[0.50,0.50]`) and never
touch crossed books, negative overround, kind ambiguity, or rounding direction.

Sweep sizes run: ~487k (american odds × edge) cells for Kelly rounding;
~537k for quarter-Kelly double-rounding; ~2k assessments for edge/Kelly
divergence; plus targeted boundary probes.

## Families hunted

- **Family 3 (absence treated as success)** → M1/M1b/M1c/M6: a book whose two
  sides cannot belong to one market is devigged as confidently as a healthy one.
- **Family 6 (direction of error)** → M2/M2b/M5: `round()` raising stakes.
- **Family 2 (same rule in disagreeing copies)** → M3: "market price" exists
  twice inside one assessment and disagrees with itself.

---

## CONFIRMED DEFECTS (each pinned by a failing test in
`tests/test_redteam_money_path.py`; 7 fail against current code, 174 existing
money-path tests still pass)

### M1 · CRITICAL — crossed / stale-mixed books devig silently
`devig_market` validates only positivity of odds, never the sign or magnitude
of overround. A crossed book — yes_ask=0.60 with no_ask=0.61, overround −0.19,
which is arbitrage-grade nonsense or two snapshots mixed by a stale cache —
returns fair probabilities [0.494, 0.506] with `"method": "power"` and no error.
The production wiring that feeds it (`tools/domains/kalshi/market.py`
`market_quote()`, built from independently-parsed yes_ask/no_ask fields) makes
this reachable from a live payload. **A crossed book must raise, not devig.**

### M1b/M1c · HIGH — assess_edge has no overround sanity gate
`assess_edge` consumes whatever `fair_probability()` returns. The audit dict
even *records* `overround: -0.19` while emitting a fair price and full Kelly
sizing from it. Flipped to the other side, the identical defect manufactures a
12.6-point phantom edge with Kelly at the 0.25 cap and `actionable=True`.
Wanted: refuse any quote whose overround ≤ 0 or > 0.5 (no real two-way book
holds 50%). This is Family 3 exactly: an input that should be impossible was
treated as trusted.

### M2/M2b · HIGH — `round()` raises Kelly fractions (486,921 sweep cells)
`kelly_full(edge, american)` rounds the exact fraction to 6 dp; Python
banker's rounding rounds UP whenever the 7th digit exceeds 5. The property
"reported stake ≤ exact stake" fails in ~487k of ~1.9M parameter cells
(e.g. edge=0.0005 at +9090: reported 0.000506 vs exact 0.0005055).
Quarter-Kelly compounds it: 537k more cells where double rounding pushes the
fraction above exact/4. Same family as the confidence round-up found by the
8,956-case sweep — this time the number raised is money-at-risk, not
confidence. Fix direction per PATTERNS.md §6: round DOWN (or truncate) any
value that increases exposure.

### M3 · MEDIUM-HIGH — edge vs devigged fair, Kelly vs raw payout
Inside one `EdgeAssessment`, `edge` subtracts the DEVIGGED market probability,
but `ev_per_unit`, `kelly_fraction_full`, and `_to_decimal(quote.price)` all
use the RAW implied payout. Result: 15+ sweep cases where `edge < 0` (no edge
by the module's own definition) yet `kelly_fraction_full > 0` — e.g.
calibrated=0.45 at +150 vs counter −104: edge −0.004, Kelly 0.083. The
actionable flag happens to hold because it reads `edge`, but any downstream
consumer sizing off `kelly_fraction_quarter` gets a positive stake on a
claim the module says has no edge. Two copies of "the price," disagreeing.

### M4 · MEDIUM — auto-kind misreads cent-quoted contracts
`MarketQuote(price=47)` with default kind='auto' interprets a 47¢ Kalshi/
Polymarket contract as decimal odds 47 → implied probability 2.13% instead of
47%. A factor-of-22 error in the market probability, silent, direction
depending on which side you back. The Kalshi adapter correctly emits
kind='probability', but nothing stops a second adapter from passing cents
with kind unset — the convention is enforced by caller discipline, not code.
(Family 4: a spelling decides a trust outcome.) Also: integers ≥100 with
|v|≥100 are *always* read as American odds; $1.10-as-110 is unreadable but
accepted without complaint.

### M5 · LOW-MEDIUM — summary() can round edge upward
`summary()` uses plain `round(x, 6)`; e.g. raw edge 6.00000017e-07 reports as
1e-06 — rounded UP. Same family as M2; fix by flooring toward zero for values
that flatter the claim.

### M6 · MEDIUM — CLV accepts a crossed book on one side
`clv_points` checks only `audit["devigged"]` — and a crossed book IS
devigged=True (M1). Stale-crossed claim quote + healthy close quote yields a
signed CLV (−4.83 bps in the probe) that would feed track-record scoring.
Since CLV gates promotions, a corrupted claim book poisons the grade rather
than being refused. Wanted: None when either side's overround is out of range.

## What did NOT break (serious attempt, honest result)

- Devig formulas themselves: multiplicative/power/shin reproduce their
  documented verifications across random healthy two-way books; power and shin
  solvers converged on every input tried including extreme longshots (odds up
  to 1e4); additive fallback never produced negatives.
- `calculate_units` correctly refuses negative edges (0 units, NO_BET) even at
  confidence 0.95.
- `kelly_dynamic` hard cap (5%) and tier multipliers behaved monotonically;
  UNVERIFIED tier correctly zeroes stake across the sweep.
- `ruin_probability` analytical/simulation agreed in direction on sampled
  inputs; negative-EV path fails closed.
- The quarter-Kelly cap `MAX_FRACTION_FULL_KELLY=0.25` held everywhere.

## Reproduce

```
python3 -m pytest tests/test_redteam_money_path.py -v
# 7 failed (defects), 3 passed (documented behaviour)
python3 -m pytest tests/test_devig.py tests/test_build_r5_edge.py \
  tests/test_tier0_money_kelly.py tests/test_tier0_money_executor.py \
  tests/test_tier0_money_sizing_and_units.py tests/test_edge_confidence.py -q
# 174 passed — no existing coverage notices these defects
```

## Recommended fix order

1. Overround gate in `devig_market` (raise on ≤0 or >0.5) — closes M1, M1c, M6.
2. Floor-toward-zero rounding in kelly_full/kelly_fractional/summary — closes M2, M2b, M5.
3. Compute Kelly/EV from the same probability the edge was measured against — closes M3.
4. Require explicit `kind` when price ∉ (0,1] — closes M4.
