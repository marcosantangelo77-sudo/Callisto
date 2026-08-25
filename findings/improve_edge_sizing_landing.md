# IMPROVE — edge quantification & sizing: the fix that never reached master

**Run:** 2026-08-24, ox-alpha standing-improvement role
**Area:** edge quantification and sizing (`tools/edge.py`, `tools/kelly.py`,
`tools/sizing.py`, Kalshi wiring)
**Branch:** `improve/money-path-landing` (pushed)

## The choice

The improve_* findings already covered artifacts/sandbox, the CLI (twice) and
retrodiction/calibration. Edge quantification was flagged in memory as having a
fixed ~100x EV bug — but when I reproduced it on current master it STILL failed.
That discrepancy was the whole run: **the fix existed on a side branch and never
reached master.**

## Family hunted: #2 at process scale

PATTERNS family 2 is "a fix lands in one copy while another keeps the bug." This
run found the largest instance yet — not two copies of a rule in one tree, but
an entire verified remediation branch stranded outside the integration line:

- `redteam/money-path-deep` (2026-08-24 ~02:00) fixed M2/M2b (round() raising
  Kelly stakes), M4 (47-cent contract read as decimal odds), M1c/M3/D7
  (invalid books reaching sizing), D1 (decimal odds → full-bankroll stake),
  D5 (push-Kelly wrong denominator), with 15 repro tests + findings doc,
  all committed AND pushed. Never merged.
- `improve/rotating-0823-213957` had separately fixed the kind-honouring
  payout bug (7df5f01). Also never merged.

Meanwhile `origin/master` still ships every one of those defects. I reproduced
three live before touching anything:

| probe | master behaviour |
|---|---|
| `assess_edge("k", .60, MarketQuote(price=47, kind="contract_cents", counter_price=54))` | ev_per_unit = **27.2** (true ≈ 0.277; ~100x), Kelly pinned at cap, actionable |
| `assess_edge` with `kind="american", price=-110` (the most common sports price) | ValueError crash — `_to_decimal` re-runs auto-detection, ignoring kind |
| `kelly_full(0.05, 1.91)` (decimal odds into an American-only API) | fraction **1.0** — full bankroll, silent |

Direction of error in every case: toward MORE confidence/stake. Exactly what
the architecture exists to prevent.

## What landed

Cherry-picked onto current origin/master as `improve/money-path-landing`
(6 commits, one conflict resolved by taking the redteam branch's version of
edge.py wholesale — it is a strict superset):

1. ef9968b→60bb1cf kelly quantisation rounds DOWN only (Family 6)
2. c06e10f→f6a8615 overround sanity gate, one-price Kelly/EV, cents auto-kind,
   never-up summary, CLV refuses invalid books
3. 7635ddf→be9f607 American-odds validation (D1) + exact push Kelly (D5)
4. 4bb0e8f→affa02b 15 repros + pins
5. dc74f30→8fc5f45 findings/redteam_money_deep.md
6. 7df5f01→162ea18 kind-honouring payout (fixes the american -110 crash)

Full detail of each defect is in findings/redteam_money_deep.md (now on the
branch); I verified its claims independently rather than trusting them.

## Measurement (before → after, same -k money suite vs origin/master worktree)

| suite (-k "kelly or edge or devig or clv or sizing or kalshi or money") | origin/master | this branch |
|---|---|---|
| failed | 8 | **6** |
| passed | 323 | **347** |

- The 2 net-fixed failures (M2, M2b, M4 = 3 fixes; M4b/pins account for the
  arithmetic with new passes) are exactly the quantisation/kind defects above.
- The remaining 6 failures are UNCHANGED from master: 5 are the deliberately-red
  argued-wrong tests documented in redteam_money_deep.md §M1/M1b/M3-literal/
  M5-literal/M6-literal (each asserts an arithmetically impossible invariant;
  replacement pins prove the intended invariant holds), 1 is
  test_redteam_retrieval_relevance::test_r4 — retrieval layer, pre-existing,
  owned by fix/synthesis-retrieval-repros.
- test_backtest_e2e.py: 40 passed. Sports/money suites green.
- Live probes post-fix: cents EV 0.277 (was 27.2); crossed book actionable=False
  kelly=0; decimal odds raise instead of sizing 100%.

## Follow-ups (real backlog, ranked)

1. **Merge-train gap:** two branches carrying verified money-path fixes sat
   unmerged while the train landed other work. Worth a periodic sweep:
   list branches whose diff touches tools/edge|kelly|sizing and check whether
   their repro tests fail on master. That sweep IS family-2 hunting at the
   process level.
2. **Stale-quote gate (D7 follow-up in the findings doc):** `as_of` is recorded
   but compared nowhere. Sizing has no clock-injection seam yet.
3. **clv_points side-blindness:** YES-side only, pinned as documented
   limitation; needs a `side=` parameter before any NO-side claim scores CLV.
4. The 5 deliberately-red tests should be rewritten or deleted by whoever owns
   them; they fail on their own sign error, not on the code.

## What I did NOT do

Did not touch tools/devig.py, tools/hypothesis.py, clv_tracker.py, or any
retrieval file — owned elsewhere. Did not merge to master myself; the branch is
landed, tested and pushed for the train.
