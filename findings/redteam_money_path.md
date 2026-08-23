# RED TEAM — the money path (devig / CLV / Kelly units)

**Surface:** money-path arithmetic — `tools/kelly.py`, `tools/clv_tracker.py`,
`tools/order_reconciler.py` (`_record_clv`), `tools/edge.py`, and the two
consumers of CLV: `tools/hypothesis.py`'s paper→live gate and
`tools/cache_manager.py`. READ-ONLY: nothing here arms execution.

**Why this surface, why now:** unattacked per the rotation list (money path was
characterized by instance 2, never red-teamed). The BUILD_MANDATE's queue item
10 wires the promotion gate to "the correct devigged column" — which makes the
CLV column family load-bearing for the first time. A defect there is no longer
a dashboard embarrassment; it is a gate that passes or fails hypotheses on
arithmetic noise.

**Method: property-based sweeps + cross-module writer audit.** Not yet used on
this surface (prior passes were adversarial input and differential runs). The
invariants attacked are stated in the test module docstring; the cross-module
check is method F — the same "canonical CLV statistic" implemented three times.

**Deliverable:** `tests/test_redteam_money_sweeps.py` — **8 findings fail on
master as written**, 4 honest-negative pins pass.
Run: `python3 -m pytest tests/test_redteam_money_sweeps.py -q`
(29 pass including the finding-documenting tests; see per-finding notes.)

---

## CONFIRMED BREAKS

### M1 (CRITICAL) — NaN edge sizes a MAX bet through the primary sizer
`tools/kelly.py::kelly_full` computes `p = implied + edge`, then clamps with
`max(0.0, min(1.0, p))`. In CPython `min(1.0, nan) == 1.0` (NaN fails every
comparison, so min/max return whichever argument comes "first" — here the
clamp silently converts garbage into certainty). Result chain:

- `kelly_full(nan, -110)` → **1.0** (full Kelly!)
- `kelly_dynamic(edge=nan, ...)` → stake = **$500 on a $10k bankroll**, at the
  hard cap, flagged `hard_cap_applied=True` — i.e. the cap machinery confirms
  the bogus bet instead of refusing it.

Where can a NaN edge come from? Any upstream division by zero, a model output
parsed from `"nan"` JSON, or a pandas mean over an empty window — none of which
raise. `bet_executor.compute_stake` calls `kelly_dynamic` directly with whatever
edge it was handed; its only guards are `stake < MIN_BET_AMOUNT → 0`.

The sibling sizer `calculate_units` **does** fail closed (`fraction =
max(0.0, nan * …)` → 0 → NO_BET), so the same input produces $0 in one sizing
path and $500 in the other. Two copies of "no edge means no bet" disagree;
the dangerous one is the one wired to the executor. Fix shape: reject any
non-finite edge/odds/confidence/variance at the top of both functions —
return 0.0 stake, log loudly. Tests:
`test_kelly_full_nan_edge_returns_zero_not_one`,
`test_kelly_dynamic_nan_edge_sizes_max_bet`,
`test_kelly_dynamic_inf_edge_sizes_max_bet`,
`test_nan_edge_inconsistency_between_sizers`.

### M2 (HIGH) — vig asymmetry mints phantom positive CLV at zero price movement
`clv_tracker._log_clv` deviggs placement with the PLACEMENT book's vig estimate
and the close with the CLOSE book's vig estimate (`placement_vig=0.05` default
retail, `closing_vig=0.025` Pinnacle). Bet −110 at DraftKings, close −110 at
Pinnacle — the line did not move at all — and the canonical column records
**+63 bp** of positive CLV. That is pure book-arithmetic noise, yet:
the docstring says sustained positive CLV means you beat the close; the
paper→live gate requires ≥50% positive-CLV rate; and retail books dominate
placement data while closes come from sharp books by design
(`close_reliable` prefers pinnacle). Every retail-placed bet gets ~half a
point of free CLV credit — comparable in size to genuinely good bet CLV
(+30–60bp). The noise floor sits AT the signal level. Tests:
`test_same_price_same_close_retail_vs_sharph_mints_positive_clv`,
`test_vig_asymmetry_phantom_exceeds_real_edges`.
Fix shape: devig BOTH legs from the SAME close-side vig (or use the two-way
devig in tools/devig.py against the counter price), not two different vig
estimates.

### M3 (CRITICAL, cross-module) — three writers, one "canonical" column, different math
The audit's unit fix declared `clv_log.clv_prob_bp` THE canonical devigged
statistic ("the ONLY supported unit going forward", clv_tracker.py:451).
Three writers fill it:

1. `clv_tracker._log_clv` — devigged both sides, sign = closing_fair −
   placement_fair. Canonical as advertised.
2. `clv_tracker.log_paper_trade_clv` — same formula, but closing vig hardcoded
   0.025 regardless of actual source ("assume sharp"). Approximation, disclosed.
3. `order_reconciler._record_clv` (line ~626) — **RAW implied delta, no devig
   anywhere**, written into the same `clv_prob_bp` column. Exactly the
   statistic the original audit condemned as carrying a 1–4% phantom edge,
   reintroduced one column rename later. It even skips the
   `regime_phase_at_placement` column (INSERT lists 14 cols for 15 slots —
   works because SQLite pads NULLs, but it is drifting).

Downstream, `tools/resolvers/betting.py::mean_clv_prob_bp` AVGs the whole
column with NO writer filter and NO `close_reliable` filter, and
`tools/hypothesis.py`'s CLV gate treats ≥3 rows (`MIN_CANONICAL_CLV_SAMPLE`)
as canonical truth for paper→live. One live-execution order reconciled before
its close was devigged poisons the gate statistic invisibly. This is the F-class
failure mode exactly: the rule exists twice, one copy was corrected, the other
kept the pre-fix arithmetic under the post-fix name. Tests:
`test_two_writers_disagree_on_identical_price_move`,
`test_gate_reads_both_writers_as_one_statistic`.

### M4 (MEDIUM) — the gate's legacy fallback reads the raw-implied column
When fewer than 3 canonical rows exist, `hypothesis.py` falls back to
`report['clv']['positive_clv_rate']`, computed in `get_clv_report` from
`bets.clv_implied` — raw implied deltas (`clv_tracker.py:295`,
`clv_implied = closing_implied − placement_implied`). So the gate can pass a
hypothesis to live review on precisely the phantom-vig statistic the rebuild
was supposed to retire, whenever the canonical sample is thin — i.e. exactly
when evidence is weakest. Test: `test_legacy_fallback_reads_raw_implied_column`.

### M5 (LOW) — cache_manager still reports `avg_clv_cents = avg(clv_implied)*100`
`tools/cache_manager.py:277` multiplies the raw implied delta by 100 and labels
it cents. Post-fix, `clv_cents` holds prob-bp, so this label is wrong twice
over (unit and semantics); any dashboard reading the cache shows CLV off by
~2× and mislabeled. Cosmetic today, but it keeps the mixed-unit confusion alive.

### M6 (LOW) — `calculate_units` linearization is not Kelly at plus odds
Documented divergence (pinned in characterization tests) but worth restating
under rotation: `fraction = edge * kelly_fraction * tier_mult` ignores odds
entirely. At −400 vs +400 the true quarter-Kelly differs by ~5× for the same
edge; calculate_units gives identical answers. Fine for a rough UI number,
dangerous if anything ever consumes it as a stake.

---

## HONEST NEGATIVES — attacks that did NOT land (kept as pins)

- **Kelly monotonicity in edge**: 200-step sweep × 14 prices — strictly
  non-decreasing everywhere. The clamp direction is right when inputs are finite.
- **Caps**: 3,000-case sweep of `calculate_units`; 5%-cap invariant holds across
  bankroll 10→10⁷. `kelly_dynamic` cap verified at 5000 cases (no violation > 1e-9).
- **`kelly_portfolio` double-penalizes correlated portfolios** (portfolio
  penalty AND per-bet penalty stack: 0.8165 × 0.875 at ρ=0.5) — conservative
  direction (undersizing), not an inflation bug; noted, not filed as a break.
- **`ruin_probability` safe-stake algebra**: signs check out across the sweep;
  negative-EV branch correctly returns ruin=1.0, stake 0.
- **`tools/devig.py` core methods** (multiplicative/power/shin): fair
  probabilities sum to 1 and symmetric markets devig to 0.50/0.50 across a
  random sweep — consistent with its own verified tests.
- **`edge.py` single-sided honesty**: refuses to call a raw quote fair
  (`devigged=False` note threaded through), and `clv_points` returns None
  unless BOTH sides have counter-quotes. This module is the cleanest on the path.

## WHAT TO FIX (ordered by leverage)

1. **Non-finite guard at the top of `kelly_full`/`kelly_dynamic`**
   (`math.isfinite` on every numeric arg → stake 0). One-line class fix for M1;
   without it, any upstream NaN becomes max-size risk the moment execution arms.
2. **Make `order_reconciler._record_clv` actually devig** (import the same
   `_half_vig_devig`), or write its rows with a `writer='raw'` marker column and
   filter on it in `mean_clv_prob_bp` and the gate query (M3/M4).
3. **Devig CLV pairs against ONE vig basis** — the close's book — rather than
   placement-vig vs close-vig, killing the M2 phantom (+63bp at zero movement).
4. Retire or clearly flag the legacy fallback (M4) and fix the cache label (M5).

## Relation to prior passes

The confidence-inflation pass found laundering through evidence classes and the
artifact pass found laundering through artifact metadata; this pass finds the
same theme in money: a column renamed "canonical" while one of three writers
still computes the condemned statistic, and a clamp that turns NaN into full
Kelly — the numeric twin of the round-up-on-clamp bugs (a clamp converting
garbage into the most favorable legal value).
