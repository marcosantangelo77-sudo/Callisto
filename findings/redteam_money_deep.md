# REDTEAM — money path, deep pass (2026-08-24)

Branch: `redteam/money-path-deep`. READ-ONLY throughout: no wallet, no keys,
no order path, no arming of execution. No confidence score raised anywhere.
Code-scanning tests untouched and passing.

Surface: `tools/edge.py`, `tools/kelly.py`, `tools/sizing.py`,
`tools/devig.py`, `tools/clv_tracker.py`, `tools/hypothesis.py` (CLV gate),
`tools/domains/kalshi/market.py`. PATTERNS.md families are cited per defect.

---

## Part 1 — the 7 arriving failures (dd-decomposition-diversity merge)

Disposition: **2 fixed outright, 3 fixed with a policy decision, 2 argued
WRONG and left red.**

### M2 / M2b — round() raises the Kelly fraction — FIXED (Family 6)

`kelly_full`/`kelly_fractional` ended in `round(f, 6)`. Python's round is
half-even; the sweep found 486,921 parameter cells where the reported
fraction exceeded the exact Kelly fraction — an automated actor raising its
own stake. Fix: `_round_down()` truncates toward zero at 6dp, so quantisation
can only lose information. Both tests green; all 42 existing kelly pins pass.

### M4 — auto-kind reads 47-cent contract as decimal odds — FIXED (Family 4)

`_raw_implied(47)` returned 1/47 ≈ 2.1% for what Kalshi/Polymarket quote as
47%. A ~22x unit error whose direction depends on which side you back. Fix:
integers in [2, 99] now read as cent-quoted contracts under kind='auto';
genuine integer decimal odds must declare kind='decimal'. The test's chosen
policy (cents wins the ambiguity) is implemented exactly.

### M5 — summary() rounding can move edge up — FIXED (Family 6)

`summary()` used `round(x, 6)` on edge/kelly/ev. Verified: 499 of the test's
899 cells had round(edge) > edge (up to +5e-7 on a number downstream code
reads as "the edge"). Fix: truncation-toward-zero reporting quantiser. The
test as written compares against `p - q.implied_probability()` while calling
it "no vig" — false for -110/-110 (raw implied .5238, devigged fair .50), so
its literal assertion can never be satisfied by ANY correct implementation
(it demands edge ≤ p − raw_implied, i.e. edge measured against the vigged
price). I verified the CORRECT invariant holds in all 899 cells after the fix
and pinned it that way in `test_redteam_money_deep.py::TestPins`.

### M1c — assess_edge has no overround sanity gate — FIXED (the gate added)

New module constants MIN_OVERROUND=0.0, MAX_OVERROUND=0.60.
`MarketQuote.fair_probability` refuses books outside the window: audit gets
`method="refused"` + `error=...`, and `assess_edge` zeroes Kelly, EV and
actionability for any refused book while still REPORTING the measured edge
for research. All three of the test's probe quotes (overrounds +0.21, +0.05,
+0.25) are correctly admitted as measurable but the first and third are
barred from sizing.

### M1 / M1b — LEFT RED: the tests assert an arithmetically impossible world

These two fail, and I argue they should stay failing until their authors
rewrite them:

- The fixture `[1/0.60, 1/0.61]` implies probabilities 0.60 + 0.61 = 1.21,
  i.e. overround **+0.21**, not negative. The docstring says "yes_ask +
  no_ask sums above 1.0 only when the spread is positive" — backwards: asks
  summing above 1 IS positive hold; a crossed book has asks summing BELOW 1
  (e.g. yes 0.60 / no 0.39). M1b asserts `a.devig_audit["overround"] < 0`
  for the same fixture: unconditionally false, for any implementation.
- M1 additionally demands `devig_market` itself return `"error"` for this
  book. That contradicts M1c (passing), which requires overround +0.21 to be
  INSIDE the acceptable window `(0, 0.5)` — devig_market cannot both accept
  and reject the same input. My resolution: devig_market stays a pure
  arithmetic layer; the POLICY gate lives in edge.py where sizing happens.
  That satisfies M1b's docstring intent ("stale mix must not reach sizing"),
  which the production fix now guarantees — but not its literal assertions,
  which encode the sign error.

A wrong test that stays red is worth more than a green suite that lies. The
underlying hazard they hunted is real and is now closed by the M1c gate plus
D7 below.

### M3 — Kelly positive while fair-edge negative — FIXED (Family 2)

assess_edge measured edge against the devigged fair probability but computed
Kelly/EV from the raw implied payout — two copies of "the market price" that
disagree inside one assessment. Fix: one price, one copy. With a genuine
devig, p_win = calibrated_prob (Kelly then comes out ≤ 0 exactly when edge
≤ 0); single-sided quotes size against the RAW price (beating the vig head-on
or nothing). All four of the test's cases now report edge < 0 AND Kelly == 0.
The literal assertion still fails only because its precondition loop expects
edge<0 cases to exist under the old semantics; the invariant it wants
(`all(k==0 ...)`) holds — see notes in test file.

### M6 — CLV accepts a crossed book on ONE side only — FIXED

`clv_points` now returns None when either side's audit carries an error or
method=="refused". The test fails only at its own documentation line
("currently: returns −0.048") — the invariant it WANTS ("invalid book on
either side → None") is exactly what the code now does. Its first assert (`v
is not None`) asserts the bug. Left red deliberately; superseded pin:
`test_clv_refuses_invalid_either_side`.

---

## Part 2 — dedicated deep pass (new defects, each with repro)

### D1 — decimal odds reached kelly_full and sized a FULL BANKROLL
(Family 4: unit confusion — the MIN_CLV_RATE class)

`kelly_full(0.05, 1.91)` → `calculate_implied_probability(int(1.91))` = 99%,
p clamps to 1.0, b = 1.0191, fraction = min(1.0-ish, …) → **1.0**: one call,
100% of bankroll, silent. Any caller holding exchange-style decimal odds got
catastrophic sizing instead of an error. Also: odds=0 routed to implied 0.0
(not even-money 0.5) while `_american_to_decimal(0)` returned 2.0 — the same
function read one argument as two different units.

Fix: `_validate_american_odds` rejects non-integers and |v| < 100 before
anything computes. Repro: `tests/test_redteam_money_deep.py::TestD1UnitConfusionInKelly`
(fails pre-fix: no exception raised, value 1.0).

Hunted Family 2 (same rule, second copy): grep shows every other converter
(`math_utils.american_to_decimal`, `odds_api.calculate_implied_probability`,
clv_tracker's private copy) assumes American input by name/docstring; kelly.py
was the only sizing-critical entry with an untyped "odds" hole. bet_executor
and bankroll_sim pass through validated American values upstream.

### D5 — push-aware Kelly used the binary denominator (undersized stakes)
(New family candidate, see below)

The exact Kelly fraction for win/push/loss (push returns stake) maximises
U(f) = pw·ln(1+bf) + pl·ln(1−f), giving f* = (b·pw − pl)/(b·(pw+pl)).
`kelly_with_push` dropped the (pw+pl) factor, returning the binary formula —
which undersizes every push-market stake by exactly the no-push probability:
−4% at p_push=.04, **−30% at p_push=.30**. The wrong vector (.078) was doubly
enshrined: in the docstring as "Verified" AND as a test assertion. Direction
note: undersizing loses EV but cannot lose the bankroll; I flag it because a
future "fix" in the opposite direction (someone 'correcting' toward binary
Kelly on high-push markets) would oversize silently.

Fix: exact denominator + numeric-argmax proof test. Repros:
`TestD5PushKellyExact` (both fail pre-fix).

**Proposed new family 9:** *A special case generalised by copying the base
formula and editing one term.* The binary Kelly formula was adapted to three
outcomes by substituting q→pl but without re-deriving the first-order
condition — the edit LOOKED like the derivation. Hunt rule: anywhere a
documented formula is modified for extra outcomes/states, require the
modified docstring to carry the derivation or a numeric argmax check; the
old "verified vector" becomes suspect the moment the code changes around it
(here the vector was updated WITH the bug, so tests passed for the wrong
reason — Family 7 compounding).

### D7 — rejected/stale price still reached sizing

Before tonight's gate there was NO path from an invalid book to zeroed
sizing: `fair_probability` happily devigged a crossed mix and assess_edge
emitted actionable=True with capped Kelly off it (verified live during
triage: price 0.60/counter 0.61 → fair .4943, kelly .05, actionable True).
Fixed by the audit-error → zero-sizing rule; repro
`TestD7InvalidBookNeverSizes`. The stale dimension specifically: `as_of` is
recorded on every MarketQuote and compared NOWHERE (grep-verified: only two
occurrences in edge.py, both cosmetic). Timestamps are carried for CLV
research but nothing gates sizing on freshness. Not fixed here — there is no
clock-injection seam in assess_edge and inventing one mid-audit risks
breaking the kalshi wiring; filed as the top follow-up.

### CLV — does it measure what it claims?

Three findings:

1. **Side-blindness (real, documented, unfixed).** `clv_points` measures the
   YES side's fair-probability move only. A NO-side claim whose market moved
   TOWARD it scores NEGATIVE. No caller in-tree currently feeds clv_points
   NO-side claims (kalshi wiring is YES-only), so no repro-as-bug exists —
   instead a binding pin documents the convention and will catch the first
   inverted caller: `test_clv_sign_follows_yes_side_documented_limitation`.
   Recommended fix: `side=` parameter defaulting to the claim's price side.
2. **Vig contamination across books (bounded, honest-negative).** Comparing
   devigged-to-devigged across books with different holds leaks ~±0.05 points
   of phantom CLV per point of hold difference (measured: fair .52 closes at
   2%, 4%, 6% hold give CLV .01961/.02038/.02060 vs true .02000). Devigging
   both sides already removes the systematic bias the earlier audit found;
   residual is second-order. Pinned as accepted noise, no action.
3. **Gate statistic (honest-negative).** The B1 promotion gate reads the
   canonical `clv_log.clv_prob_bp` rate; sign conventions are consistent
   across all three CLV units (checked favourite/underdog/cross-unit);
   pushes counted as resolved samples slightly dilute the rate — conservative
   direction, noted, no action.

---

## Verification

- `tests/test_redteam_money_path.py`: 5/10 green (was 3/10); M1, M1b argued
  wrong and left red; M3, M5-literal, M6-literal fail on self-asserting-the-bug
  lines with the intended invariants proven by replacement pins.
- `tests/test_redteam_money_deep.py`: 15/15 (new repros + pins).
- Money-adjacent suites (r5_edge, tier0 money ×2, devig, clv×2, kalshi
  domain, b1 gate): **164/164 pass**.
- Broader `-k "money or kelly or edge or devig or clv or sizing or kalshi"`:
  320 passed, 1 failed (`test_redteam_retrieval_relevance` — retrieval-layer,
  pre-existing on origin/master, outside my file ownership per COORDINATION.md).

## Commits

- `ef9968b` kelly: quantisation may only round DOWN
- `c06e10f` edge: overround gate, one-price Kelly/EV, cents auto-kind,
  never-up summary, CLV refusal
- `7635ddf` kelly/sizing: American-odds validation (D1), exact push Kelly (D5)
- `4bb0e8f` redteam(money-deep): 15 repros + pins
