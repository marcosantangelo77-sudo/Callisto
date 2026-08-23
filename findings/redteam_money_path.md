# RED TEAM — the money path (devig / EV / Kelly / CLV)

**Surface:** `tools/devig.py`, `tools/ev.py`, `tools/kelly.py`,
`tools/sizing.py`, `tools/edge.py`, `tools/clv_tracker.py`,
`tools/order_reconciler.py`, `tools/cache_manager.py` — READ-ONLY; nothing
was armed, no execution path touched, no live API called.

**Why this surface:** unattacked ground. The rotation list showed four passes
on resume/checkpoint variants and recent hits on artifacts and confidence,
while the arithmetic that every position-size decision rests on had zero
adversarial pressure since construction. BUILD_MANDATE calls Kelly "provably
correct" — a claim worth attacking directly.

**Method:** property-based sweeps over parameter spaces (the 8,956-case
confidence-sweep style — not yet used by me) PLUS differential between
independent implementations that must agree (three clv_log writers), PLUS one
recorded-scenario replay against a real SQLite tracker. Two methods new to
this rotation combined.

**Family hunted:** #6 (rounding/direction of error), #2 (same rule in two
copies), #3 (absence treated as success), #5 (structural property standing in
for agreement).

**Deliverable:** `tests/test_redteam_money_path.py` — **11 fail on master**,
15 honest-negative pins pass. Run:

    python3 -m pytest tests/test_redteam_money_path.py -q

Committed as 4bf3362 on closeout2.

---

## CONFIRMED BREAKS

### M1 (HIGH) — `kelly_full` rounds UP: automated stake inflation, family #6 again
`tools/kelly.py:171`: `round(fraction, 6)` rounds half-up. A 20,000-case sweep
found **12,108 cases where the returned fraction exceeds the exact Kelly
fraction** (e.g. edge .0068 @ +110: returns 0.012982 vs exact 0.01298182).
This is the *identical* bug class fixed in `clamp_with_ensemble` with
`math.floor` — the fix landed in one copy and not its sibling (family #2).
Each call inflates the stake by up to +0.00005% of bankroll for free.
`kelly_fractional` inherits it.

### M3a (CRITICAL) — Portfolio Kelly treats perfectly-correlated duplicates as diversification
Two bets at `correlation_with_others=1.0` (the same position listed twice)
receive **2× the allocation of one bet**: penalty `1/sqrt(1+(N-1)*rho)` =
1/sqrt(2) ≈ 0.707 per bet → total = 1.41× single-bet sizing. Ten rho=1.0
duplicates get 0.0398 total vs 0.168 for ten independent bets — stacking one
edge ten times earns you ~24% of full independent exposure instead of one
bet's 5% cap. The docstring says "perfectly correlated bets should be treated
as ONE position"; the code does the opposite. The 20%-portfolio cap is also
computed AFTER the penalty, so correlated stacks land under it.

### M3b (MEDIUM) — Negative correlation is clipped to zero
`np.clip(correlations, -1, 1)` then `max(0.0, ...)`: a hedged book (rho=-1)
receives **byte-identical allocation to an uncorrelated book** (verified:
0.0336 both). Hedging gets zero credit anywhere in the portfolio math.

### M4 (MEDIUM) — `ruin_probability` boundary crash + formula understates risk
- At exactly zero EV (`win_rate*b == q`, e.g. wr=.60 @ -150) the function
  takes the neg-EV branch via float `-0.0` and **omits `risk_level`,
  `recommended_max_stake_pct`, and the safe-stake fields** — callers KeyError
  (one did in my probe).
- Analytical vs simulated differential: analytical ruin 0.2724 vs simulated
  0.2434 (ok direction) but 0.0528 vs 0.0411 elsewhere — the closed form is
  systematically above simulation here but the ratio drifts ~30%; more
  importantly the "recommended_max_stake" derivation reuses the same
  approximation, so the safety number inherits whatever bias the boundary
  doesn't crash first.

### M5 (LOW, documented gap) — `calculate_units` ignores price entirely
`fraction = edge * kelly_fraction * tier_mult`. No odds term: a 3-point edge
at -400 and at +400 produce identical stakes. The docstring admits it ("edge
alone is ambiguous without odds") and ships it anyway. Any caller using the
convenience path rather than `kelly_dynamic` sizes off a linearized Kelly
that is wrong everywhere except near -110.

### M6 (HIGH) — `MarketQuote` auto-detection misreads prediction-market contracts
`_raw_implied`: an int with |value| ≥ 100 is American odds; otherwise
"continuous". So a Kalshi/Polymarket contract priced at **50 cents parses as
DECIMAL ODDS of 50 → implied probability 0.02**, not 0.50. And a $1.00
contract (100 cents) parses as American +100 → even money, implied 0.50.
End-to-end: `assess_edge("t", 0.90, MarketQuote(price=50))` returns
**edge=0.88, actionable=True, kelly_full=0.25 (capped)** from a contract the
market prices at exactly fair. The module docstring explicitly advertises
contract-cents support. The correct interpretation needs `kind=` and nothing
validates auto against kind.

### M7 (MEDIUM) — `evaluate_edge` accepts impossible probabilities
`evaluate_edge(1.5, -110)` → rating STRONG, actionable=True, EV +186%.
`evaluate_edge(-0.2, -110)` → NO_EDGE but no error. `assess_edge` validates;
its older sibling in `ev.py` does not, and `orchestrator.py:923` exposes it
directly as a model-callable tool — malformed model output becomes a STRONG
recommendation.

### F2 (HIGH — family #2/#5) — three clv_log writers, two units, one gate
The B1 rebuild declared `clv_log.clv_prob_bp` "CANONICAL … devigged … the ONLY
supported unit." Three writers fill that column:

| writer | computation |
|---|---|
| `clv_tracker._log_clv` | devig both sides (half-vig), canonical ✓ |
| `clv_tracker.log_paper_trade_clv` | devig both sides, canonical ✓ |
| `order_reconciler.py:627` | **RAW `(closing−placement)·10000`, no devig** |

The reconciler writes raw deltas into the same column the promotion gate
(`hypothesis.py:1228–1251`) reads as canonical devigged bp whenever the bet_id
prefix matches. Consequences measured: same number on a high-vig placement
book vs sharp close scores **0bp raw vs +63bp canonical**; across 26,244
odd-pairs the two formulas disagree on SIGN **412 times**. The gate's
positive-rate statistic silently changes definition depending on which writer
produced the row.

Also family #2: `close_reliable` — clv_tracker trusts canonicalized
{pinnacle, lowvig.ag, circa, betfair_exchange}; order_reconciler:624
hand-rolls `.lower() in {"pinnacle","circa"}` against the RAW string. LowVig
and Betfair closes are reliable on one path, unreliable on the other.

### F10 (HIGH — family #3 variant) — closing-line matching has no point guard
`record_closing_line` updates bets on `event_id + market + LOWER(team) +
result='pending'` — **no `placement_point` match**. Reproduced against a real
temp SQLite: bets at Lakers +3.5 and Lakers −2.0 are BOTH stamped with a
single −105 close recorded at 7.5 (a line neither bet was on); both get
identical `clv_prob_bp` (−51.5). A moneyline bet on the same team escapes only
because its market string differs. Every spread/total CLV in the bets table is
plausibly matched to the wrong closing number whenever multiple lines exist —
which is always.

### F11 (MEDIUM) — dashboard/context layers still read the legacy column
`cache_manager.query_clv_summary` aggregates `bets.clv_implied` (raw
implied-delta, mixed-units history acknowledged in clv_tracker's own comment)
and labels it `avg_clv_cents`; same for the Hermes memory context block
(`hermes_memory.py:519`) and `get_clv_report`. The canonical-unit migration
fixed the writers' new rows and the promotion gate — not the readers feeding
agent context and dashboards.

---

## HONEST NEGATIVES (attacks that did NOT land — pins kept)

- `timing_value` never issues WAIT into negative expected value (1,500 random
  cases). Its heuristic asymmetry is ugly but fails safe.
- `sizing.kelly_binary` agrees with `kelly_full` to 1e-6 across the sweep.
- Shin/power devig sum to 1 within 1.07e-5 over 3,000 random markets; shin
  fallback fires only at extreme (>21% hold, 90% favorite) books.
- `best_price` picks the higher decimal correctly.
- Push-aware EV matches its docstring example.
- Multiplicative devig preserves outcome ordering.
- Power devig moves the favorite the right way on heavy favorites.

## WHAT I TRIED AND COULD NOT BREAK

- `assess_edge` itself: input validation, cap direction, quarter-Kelly
  derivation all held; the defect lives in MarketQuote's parser upstream (M6),
  not in the assessment math.
- `_brentq` root finder: no bracket violations or wrong-root convergence found
  in the sweeps run through it.
- The B1 gate logic reading canonical bp: given honest canonical rows, the
  rate-vs-mean comparison is sound. The corruption enters at the writer (F2),
  not the reader.

## FAMILY SUMMARY

Every confirmed break maps to a known family: M1 = #6+#2 (rounding up; fix
landed in one copy), M3 = #4 (a structural parameter standing in for actual
diversification), M4 = #3 (boundary absence treated as success), M6/M7 =
#4 (a string/int convention deciding a trust outcome), F2 = #2+#5, F10 = #3
(missing field never fails the match), F11 = #2. No NEW family proposed —
but note the recurring meta-shape: the money path was "verified" by unit
tests written against each module's own vocabulary, and the cross-module
contracts (column units, reliability sets, point matching) had no owner.
