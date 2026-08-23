# RESTORE NOTE — attic/local_kelly/

Quarantined 2026-08-23 (branch `build/cli-front-door`, edge-sizing pass).

## What was here

The body of `local_kelly()` from `tools/local_compute.py` — a second,
independent Kelly implementation, verified dead repo-wide (ARCHITECTURE_MAP
§2.1 listed it in the 64 high-confidence dead functions; re-grepped this
pass: zero references outside its own file and no test).

## Why it was removed rather than left

It was not merely unused; it was WRONG in the exact way this system's
money-path is designed to prevent:

    implied_prob = 1 / decimal_odds        # raw implied — includes vig
    fair_prob = implied_prob + edge        # adds "edge" on top of the vig
    full_kelly = (b*p - q) / b             # sizes on the contaminated prob

No devig anywhere. On a standard -110/-110 market that bakes ~1.9 points
of phantom probability into every stake it would have sized. It also had
no Kelly cap, no claim-time price record for CLV grading.

The canonical implementation is `tools/edge.py::assess_edge`
(devigged MarketQuote → capped Kelly fractions → EdgeAssessment with the
claim price carried through), covered by tests/test_build_r5_edge.py and
tests/test_tier0_money_*.py.

## Replacement

    from tools.edge import MarketQuote, assess_edge
    a = assess_edge(claim_id, calibrated_prob,
                    MarketQuote(price=-110, counter_price=-110))

## The stub

`tools/local_compute.py::local_kelly` remains as a shim that raises
NotImplementedError pointing here, so a stale caller fails loudly instead
of silently mis-sizing.

## Restore

    git log --diff-filter=M -- tools/local_compute.py   # find pre-quarantine commit
    # restore the old function body if ever genuinely needed — but fix the
    # devig first.
