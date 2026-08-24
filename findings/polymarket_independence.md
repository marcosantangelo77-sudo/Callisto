# Finding: is a Polymarket price independent of a Kalshi price?

**Status: declared ONE overlap family (`prediction-market` in
tools/sources/base.py INDEPENDENCE_FAMILIES) — i.e. NOT counted as two
independent sources — until evidence shows otherwise.**

## Why they might be independent

- Different venue, different regulatory regime: Kalshi is CFTC-regulated,
  USD-collateralised, US-person legal; Polymarket is an on-chain venue
  settled by UMA's optimistic oracle, historically geo-fenced from the US.
- Different participants: Kalshi skews US retail and macro desks;
  Polymarket skews crypto-native and global flow.
- Different contract sets: Kalshi concentrates macro prints (CPI, Fed,
  weather); Polymarket covers crypto, geopolitics, culture, science
  milestones — most contracts have no counterpart at all.
- Different resolution mechanisms (exchange settlement vs UMA dispute).

## Why they are probably NOT independent where it matters

- Both price the SAME underlying event. When both list "Fed cuts in
  September", the quantity being estimated is identical; agreement between
  the two prices measures the same belief, sampled twice through venues
  that read the same news cycle.
- Arbitrage links them mechanically. Cross-venue traders close any gap
  beyond fees; correlated flow means divergence is quickly competed away
  and agreement is partly *constructed*, not convergent.
- Shared information set: both books move on the same FOMC statement, the
  same polls, the same wire stories within seconds.

The distinction that matters for Callisto: **agreement between two
independent estimators is corroboration; agreement between two arbitrage-
linked quotes of one estimator is nearly a single observation with extra
fees baked in.** Where the venues genuinely diverge and STAY diverged, that
is a real signal — but persistent divergence is exactly what we should log
as contradiction, not average away.

## The honest accounting rule

Conservative default (implemented): count kalshi + polymarket as one
independence family for `min_independent_sources`. The asymmetry justifies
it:

- undercounting costs a slightly lower confidence ceiling — recoverable by
  adding a genuinely independent source (a poll, an options chain);
- overcounting manufactures unearned confidence — the exact defect the
  audit found across this codebase eight times.

If later calibration shows cross-venue residuals behave like independent
noise (e.g. divergence mean-reverts to zero with uncorrelated timing), the
family can be split — but only on evidence, not on intuition.
