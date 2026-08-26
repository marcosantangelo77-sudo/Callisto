# OX TASK: auto_promote must be diagnose-only on thresholds

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-autopromote-2ac0`
Worktree: `/tmp/callisto-ox-autopromote`

## Exclusive file ownership (HARD)

You MAY edit:
- `tools/hypothesis.py`
- `tests/test_auto_promote_gate_policy.py` (create)

You MUST NOT edit any other file. Especially not `tools/autonomous.py`, `api.py`,
`tools/backtest.py`, `config/providers.yaml`, credentials, other worktrees, or `master`.

## Git rules (HARD)

- Stay on `cursor/ox-autopromote-2ac0`. Never checkout another branch.
- Do not `git stash`, `git reset --hard`, or `git checkout --`.
- Do not merge. Do not touch `master`.
- After tests pass, commit and `git push -u origin HEAD`.
- Never put secrets in commits.

## Forbidden product changes (HARD)

- Do NOT change `generate_paper_trade_signal` to accept `status == "live"`.
- Do NOT loosen promotion gates to live. The paper→live path stays hard.
- Do NOT delete `auto_promote`. Keep diagnose + existing legitimate promotion/reject.

## Bug (verified)

`HypothesisManager.auto_promote` (`tools/hypothesis.py` ~1893–1969):

When a backtesting hypothesis has events but 0 signals, and
`_diagnose_edge_threshold` says `threshold_too_high`, the method currently:

1. WRITES a lowered `edge_threshold` onto the hypothesis
2. REWRITES `backtest_events.signal_generated` to match the new threshold
3. SYNC `backtest_runs.signals_generated`
4. Then may immediately promote if the new signal count clears the gate

That is a silent gate saw: the system manufactures the evidence it needs.

## Required behavior

Keep `_diagnose_edge_threshold` and the logging. STOP the writes.

When `edge_diag.get("threshold_too_high")` is true:

- Log the current threshold, recommended threshold, max observed edge, event counts.
- Return a result dict such as:
  `{"action": "held", "reason": "threshold_too_high", "diagnosis": edge_diag}`
  (include the diagnostic fields; do not promote on this path).
- Do NOT `UPDATE hypotheses SET edge_threshold`.
- Do NOT `UPDATE backtest_events SET signal_generated`.
- Do NOT `UPDATE backtest_runs SET signals_generated`.
- Do NOT reset `evaluate_cycles` as a way to sneak another auto-lower later
  *unless* you still increment evaluate_cycles as today BEFORE the diagnosis
  (the increment that tracks "we looked" is fine; wiping it to 0 is part of
  the old auto-lower path and must go).

All other auto_promote paths (real promotion when gates pass without rewriting
events, auto-reject of untestable hypotheses, paper→live hard gates) stay.

Search the rest of `auto_promote` for other `SET edge_threshold` or
`signal_generated` rewrites. If you find more auto-lower/rewrite blocks in
THIS method, apply the same diagnose-only policy. Do not change unrelated
methods (e.g. human/operator threshold updates elsewhere) unless they are
called only from this auto-lower path.

## Tests (required)

Create `tests/test_auto_promote_gate_policy.py`.

Reuse the in-memory schema pattern from `tests/test_promotion_gates.py`
(`_setup_db`, `_make_mgr`). You will also need `backtest_runs` if your
assertions touch it — add a minimal CREATE TABLE if the manager SQL needs it.

Fixture:
- Hypothesis `status='backtesting'`, `edge_threshold=0.05` (high).
- Several `backtest_events` with `edge` in (0.02, 0.04), `signal_generated=0`,
  distinct `event_id`s.
- `model_config` JSON with `evaluate_cycles` already >= 2 so the diagnosis
  branch is reached (see the `eval_cycles >= 2` guard).

Stub `_diagnose_edge_threshold` to return
`{"threshold_too_high": True, "recommended_threshold": 0.01, "current_threshold": 0.05, "max_edge": 0.04}`
if the real diagnostic is hard to trigger; ALSO add one test that uses the
real diagnostic if it is straightforward with the seeded edges.

Assertions after `await mgr.auto_promote(hid)`:
- `hypotheses.edge_threshold` still 0.05
- every `backtest_events.signal_generated` still 0
- return `action` is `held` (or equivalent non-promote/non-reject that does
  not mutate evidence). Not `promoted`.
- hypothesis `status` still `backtesting`

Second test: when diagnosis says threshold is NOT too high and there are
truly 0 signals after many cycles, existing auto-reject behavior may still
fire — do not break that. Keep this test narrow if the reject path needs
more cycles than you want to simulate.

Do NOT run the full suite. Run:

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_auto_promote_gate_policy.py tests/test_promotion_gates.py -q
```

If the venv is missing, create one with pytest pytest-asyncio aiosqlite.

## Done

Commit message:
`fix(hypothesis): auto_promote diagnoses high thresholds without rewriting evidence`

Push: `git push -u origin HEAD`

Write `OX_DONE.md` in the worktree root with: files changed, test command + result, commit SHA.
