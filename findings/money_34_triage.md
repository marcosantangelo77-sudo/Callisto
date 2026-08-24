# TRIAGE — the money/red-team suite's 34 failures (2026-08-24)

**Run:** ox-alpha, `loop` worktree on `review/deep-audit-0824` (= `96e09c9`
landmerge + e2e-seal work). READ-ONLY throughout: no wallet, no keys, no
order path, no live execution armed, no confidence score raised, no
code-scanning guard touched. Every disposition below was **executed**, not
copied: each failing test was re-run on this tree, on a clean
`origin/master` worktree, and (where a fix branch exists) on that branch.

---

## 1. First: what the "34" actually are

The 17 branch-only red-team/tier0 money files (197 tests at the time,
`dd_instrument_decision.md` FINAL RESULTS) ran for the first time against the
merged tree on `build/dd-instrument-decision` @ `091e06b`. Result: 163 pass /
34 fail. The dd-merge notes already decomposed them:

| block | count | what happened after |
|---|---|---|
| still-open defect repros (pre-existing) | ~27 | still red today — see table |
| K1 calibration-scoring repros | 3 | **now PASS**: master fixed `_implied_outcome`'s ground-truth fabrication (fail-closed); repros became stale pins |
| retr_selection_nulls mixed-verdict collisions | 3 | resolved by policy decision toward master's disclosed-mixed rule (owner sign-off recorded in dd notes) |
| forged-amendment attack | 1 | **intentionally dead**: `score()`'s basis is now the sealed ORIGINALS (`agp/preregistration.py`, commit `24fb323`) |

On today's `origin/master` line the same 21 red-team/tier0 files carry 219
tests with **28 failures** (2 more files joined the family; several defects
were fixed and flipped to pins). Section 3 triages all 28 individually and
accounts for the delta from 34.

## 2. Claim verification: "~27 fail identically on the branch alone"

**VERIFIED, and the truth is stronger.** I ran the complete offline suite
(ml collection errors excluded, environmental xgboost/joblib) on:

- this merged tree (`96e09c9` + seal work), and
- a pristine detached worktree at `origin/master` `96e09c9`.

Failure lists are **byte-identical — 28 lines, same names, same order**:
the merge introduced ZERO new failures and fixed zero. Every failure in the
suite predates the merge; the "~27" estimate is confirmed at 100%.

## 3. The triage table

Legend: **[DEFECT]** real production defect · **[WRONG-TEST]** assertion
argued wrong, left red deliberately · **[STALE]** asserts behaviour master
already replaced · **[ENV]** environment/test-hygiene gap · **[FIXED-BRANCH]**
verified fix exists on an unmerged branch.

### Block A — money path (`tests/test_redteam_money_path.py`, M-series)

| # | test | verdict | evidence |
|---|---|---|---|
| 1 | m1 crossed-book overround negative | **[WRONG-TEST]** | fixture `[1/0.60, 1/0.61]` implies probabilities summing to 1.21 → overround **+0.21**, not negative (run 8 proved this first). Demands `devig_market` return `error` while M1c (passing) requires +0.21 inside the acceptable window — mutually unsatisfiable. Stays red. |
| 2 | m1b stale snapshot mix | **[WRONG-TEST]** | dies at its own precondition (`overround < 0` → is +0.21). Unconditionally false for any implementation. |
| 3 | m2 kelly_full never rounds up | **[DEFECT]→[FIXED-BRANCH]** | master ends `round(fraction, 6)`; 486,921 sweep cells where the stake rounds UP. Fix `_round_down()` verified on `improve/money-path-landing` @ `7d18956`: test passes there (116 passed / 5 argued-red). |
| 4 | m2b fractional double rounding | **[DEFECT]→[FIXED-BRANCH]** | same root cause in `kelly_fractional`; passes on landing branch. |
| 5 | m3 Kelly positive while fair edge negative | **[DEFECT]→[FIXED-BRANCH]** | two copies of "the price": edge measured devigged, Kelly computed at raw payout. Fix (one-price Kelly/EV) lands; test then fails only because its precondition loop expects edge<0 cases under OLD semantics — invariant it wants holds, replacement pin exists. Left red by design. |
| 6 | m4 47-cent contract read as decimal odds | **[DEFECT]→[FIXED-BRANCH]** | `_raw_implied(47)` → 1/47 ≈ 2.1% vs true 47% (~22× error). Cents-rule auto-kind fix verified passing on landing branch. |
| 7 | m5 summary round never raises edge | **[DEFECT]→[FIXED-BRANCH]** | `summary()` used half-up `round()`; literal test compares against `p − raw_implied` calling it "no vig" — false for −110/−110 (raw .5238, fair .50), so no correct implementation can satisfy it. Correct invariant pinned in `test_redteam_money_deep.py::TestPins`; passes on landing branch. |

(Block A also contributes the *passing* M1c/M4b/M6-side pins; not counted.)

### Block B — retrieval & relevance (`tests/test_redteam_retrieval_relevance.py`)

| # | test | verdict | evidence |
|---|---|---|---|
| 8 | r2 three-char prefix junk → 88% coverage | **[DEFECT]→[FIXED-BRANCH]** | bidirectional prefix matching admits 15 chars of junk. Verified fix on `fix/synthesis-retrieval-repros` @ `d2ac59a`: normalized-exact matching + plural collapse, direction-of-error documented (reject-more). R2/R2b/R1/neg-pins pass there. |
| 9 | r2b one common word admits anything | **[DEFECT]→[FIXED-BRANCH]** | type-words diluting the denominator; same branch fixes. |
| 10 | r3 engine fallback counts family members twice | **[DEFECT]→[FIXED-BRANCH]** | openalex+semanticscholar collapse to one independence unit via `independence_key`, name-set fallback says 2. Engine now derives keys through the same rule; passes on branch. |
| 11 | r3b sandbox success adds a fake voice | **[DEFECT]→[FIXED-BRANCH]** | computation counted toward `min_independent_sources`; branch removes it. Passes there. |
| 12 | r4 gate-rejected bytes mint PRIMARY | **[DEFECT]→[FIXED-BRANCH]** | promotion is now a gate VERDICT (`mark_admitted` / `_promotion_grade`, fail-closed on unjudged bytes); passes on branch. |
| 13 | r4b gate-rejected URL verifies citations | **[DEFECT]→[FIXED-BRANCH]** | `cites_verified_url` consults admitted-URL set only; passes on branch. |

### Block C — synthesis & corroboration (`tests/test_redteam_synthesis_corroboration.py`)

| # | test | verdict | evidence |
|---|---|---|---|
| 14 | s1 contradiction hidden in claim text | **[DEFECT — OPEN]** | numbers phrased inside `claim` are never extracted; 60%-vs-20% group scores full agreement. No fix branch touches it. Red is correct signal. |
| 15 | s1b max-abs cherry-pick hides/makes contradictions | **[DEFECT — OPEN]** | `max(values, key=abs)` picks $9bn revenue as "the" value. Open. |
| 16 | s1c silence treated as support | **[DEFECT — OPEN]** | stance `''` vs `"refutes"` raises no contradiction; PRIMARY+INFERRED-refutation reads 0.85. Open. |
| 17 | s2b ten mirrors of one doc score 1.0 | **[DEFECT]→[FIXED-BRANCH]** | independence keyed off mirror-controlled host names. Branch collapses single-content-hash groups to ONE voice; S2b passes there. |
| 18 | s3 report confidence = max over groups | **[DEFECT — OPEN]** | one lucky PRIMARY group outweighs INFERRED filler; parent inherits via best-leaf rule. Open (design decision needed). |
| 19 | s4 hostile mirrors read as literature null | **[DEFECT — OPEN]** | `classify_null` reports `literature_null` without naming sources' standing. Open. (Note: run 12's audit flagged the same seam from the null side.) |

### Block D — confidence laundering (`tests/test_redteam_confidence_laundering.py`)

| # | test | verdict | evidence |
|---|---|---|---|
| 20 | synthesis best-class laundering | **[WRONG-TEST]** | asserts `score == 1.0` (the bug) AND `score <= SECONDARY ceiling` simultaneously; arithmetic impossibility. Fix landed (`per-class accounting`): score == 0.70, satisfying the second line. Argument + post-fix form recorded in `findings/fix_laundering_remainder.md`. Owner must delete/invert line 1; stays red until then. |
| 21 | panel veto returns rounded-up score | **[STALE/WRONG-TEST]** | asserts `out == 0.84` AND `out <= 0.836` — unsatisfiable; the round-up it demonstrates was already FIXED (veto path floors: out == 0.83). Stale duplicate of closed F1 family. Same argument file. |

### Block E — non-money strays inside the 34-accounting (currently-failing neighbours)

| # | test | verdict | evidence |
|---|---|---|---|
| 22 | lifecycle amendment default basis | **[FIXED — was intentional-fail]** | `score()` now scores against sealed ORIGINALS unless amendment explicitly passed (`24fb323`); the forged-amendment attack is dead on the default path. Test updated accordingly upstream. On master line the residual lifecycle pair is: journal tampering detection — see #23. |
| 23 | journal hash-chain rejects retroactive edits | **[DEFECT — OPEN, CRITICAL per run 11]** | rewriting history does NOT raise ClaimError on load: chain verifies hashes but the newest entry escapes the check window (tamper-blind to newest entry). Reproduced live. Fix exists on `build/dd-instrument-decision` (`content-bound journal chain`), UNMERGED. |
| 24 | i1 engine fetches multi-source | **[ENV+DEFECT mix]** | traced live: round-1 candidates (clinicaltrials/federalregister/fred) 404 against the two-route fixture transport; round-2 admits openalex; round 3 gain-gate then skips everything as "duplicate voice". The fixture predates planner fan-out (routes only `/works` + SS paper-search). Not weakened; needs either richer fixtures or a gain-gate exemption for round-1 candidates. |
| 25–27 | p3 bea/eia/courtlistener missing-key | **[ENV]** | these tests assert SourceError when the key env var is ABSENT — but this machine exports REAL keys (`CALLISTO_BEA_API_KEY`, `CALLISTO_EIA_API_KEY`, `CALLISTO_COURTLISTENER_TOKEN`), so the adapter builds a URL and the FakeTransport correctly refuses it. `monkeypatch.delenv` hygiene fixes exist on `fix/source-repair-round2` (4 delenv sites) — UNMERGED. Environment gap, not product defect. |
| 28 | tier7 no-artifact-return-path | **[STALE characterization pin]** | asserts the string "artifact" is absent from `agp/__init__.py`; written before agp HAD an artifact layer. The artifact-seal feature is deliberate; the module header itself documents the update path. Tier-7 owner's call; left failing with argument. |

### Accounting: 34 → 28

34 (first run) − 3 (K1 repros made green by master's fail-closed fix)
− 3 (retr_selection_nulls policy-resolved) − 1 (forged-amendment killed by
prereg default-basis fix) + additions from later suites joining the family =
the 28 rows above, each individually dispositioned. Nothing was skipped,
xfailed or weakened anywhere in this triage.

## 4. D1 in context — the defect the question asked about

Confirmed live on `origin/master` bytes, executed during this triage:

```
kelly_full(0.05, 1.91)  →  1.0        # FULL BANKROLL, silent
```

Mechanism: `calculate_implied_probability(int(1.91))` truncates 1.91 → 1,
reads American +1 → implied 99%; p clamps to 1.0; b = `_american_to_decimal`
of the float → fraction saturates at 1.0. Any caller holding exchange-style
decimal odds gets catastrophic sizing instead of an error. Also: `odds=0`
routed to implied 0.0 while `_american_to_decimal(0)` returned 2.0 — one
argument read as two units.

Fix verified on `improve/money-path-landing`: `_validate_american_odds()`
rejects non-integers and |v| < 100 BEFORE anything computes; repro
`TestD1UnitConfusionInKelly` fails pre-fix, passes post-fix. **Branch is
pushed and green (116/121 with the 5 argued-red above); it needs the merge
train, not more analysis.**

## 5. Family #4 hunt: OTHER unvalidated numeric inputs on the money path

Per PATTERNS #4/#6 ("a value trusted without validation", "direction of
error"). All reproduced/read directly on master; none fixed here (ownership +
read-only mandate), each carries a prior repro in findings/.

1. **kelly_portfolio duplicate stacking (A06)** — docstring promises
   duplicates cost one position; code charges 1.414× for perfect
   correlation. Correlation input itself is taken from bet dicts unvalidated;
   NaN correlation treated as 0 in the sweep. OPEN.
2. **order_reconciler.py:627 unit laundering (F2)** — writes RAW
   `(close−place)·10000` into the canonical DEVIGGED `clv_prob_bp` column the
   promotion gate reads. Two units, one column, gate poisoned. OPEN.
3. **Closing-line point guard predicate too wide (A08/F10)** — UPDATE matches
   event/market/team only; a point-less match can overwrite the wrong line.
   OPEN.
4. **`clamp_to_ceiling` rounds UP (A01)** — memory-wiki clamp:
   `clamp_to_ceiling(0.5497, "INFERRED") == 0.55`. Same floor_conf rule that
   was applied at six OTHER sites; this copy kept the bug (family #2 in one
   function). Fix stranded on `review/rotating-0823-155500` `4352ad1`. OPEN.
5. **`inherited_ceiling` `round(min(...), 4)`** — can land ON the class-cap
   boundary from below (0.74997 → 0.75 = SECONDARY cap). Cosmetic-to-minor;
   noted by run 12. OPEN.
6. **engine round() mints tiers** — `round(0.5497, 2) == 0.55 → PROBABLE`,
   `round(0.749999, 2) == 0.75 → CORROBORATED` via engine.py:469. ba0a63c's
   floor_conf reached six sites but not this one. OPEN (HIGH, families #6+#2).
7. **`as_of` recorded but compared NOWHERE** — MarketQuote carries fetch
   timestamps; nothing gates sizing on freshness. A stale price sizes as
   happily as a live one. Filed as top follow-up by the deep pass. OPEN.
8. **odds=0 dual reading** — implied-probability path reads 0 → 0.0%, decimal
   conversion reads 0 → even money 2.0. Closed by the same
   `_validate_american_odds` fix as D1 (rejects 0). FIXED-BRANCH.
9. **cmefedfut `attach_market_implied` fail-open ×2 (A10)** — wrapper and
   missing-claim_date path bypass the W5-style guard; branch tests cover only
   the refusals beside it. OPEN.
10. **estimate sealed-number pin blind spot** — perturbing the sealed conf by
    ≤0.004 or ceiling +0.1 passes all 8 wiring tests (values sit where
    rounding absorbs them). Recommended: property sweep asserting
    `sealed == round(min(est,ceil), 2)` exactly. OPEN (test-hardening).
11. **base.py `independence_family` raw-spelling copy** — `semantic_scholar`
    falls through to itself while `semanticscholar` maps to
    scholarly-aggregator: two spellings, two verdicts, one rule (family #2).
    Latent (no production caller yet). OPEN.

## 6. Bottom line

- All 34 accounted for, row by row. 9 are verified-fixed on **two unmerged,
  pushed branches** (`improve/money-path-landing`,
  `fix/synthesis-retrieval-repros`) plus source-hygiene on
  `fix/source-repair-round2`; merging those three turns ~16 of tonight's reds
  green WITHOUT touching a single assertion.
- 5 reds are argued-wrong/self-asserting-bug tests (M1, M1b, laundering ×2,
  plus tier7's stale string-pin) — they stay red, with arguments, per the
  M1/M1b standard set by run 8.
- The remainder are REAL open defects whose repros are doing their job:
  contradiction-detection holes (S1/S1b/S1c), max-over-groups report
  confidence (S3), hostile-mirror nulls (S4), journal newest-entry tamper,
  portfolio-Kelly correlation, CLV unit laundering, engine round()-tier
  minting, and the freshness gate that doesn't exist.
- The dominant meta-defect remains PATTERNS family #2 at process scale:
  reviewed fixes piling up outside the integration line. The sweep proposed
  by improve_edge_sizing_landing (list branches touching
  tools/edge|kelly|sizing; check their repros on master) IS the fix.
