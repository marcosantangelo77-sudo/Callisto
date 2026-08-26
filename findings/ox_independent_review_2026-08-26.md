# Independent review — OX wave 1/2 finished heads (2026-08-26 04:13Z)

Orchestrator ran focused tests in each worktree. Not OX self-report.
Do not squash-merge until a human/orchestrator squash to a clean master
after this file. All four **APPROVE** for squash pending no file overlap
with still-live workers.

| Branch | SHA | Focused tests (independent) | Adversarial notes | Disposition |
| --- | --- | --- | --- | --- |
| `cursor/ox-autopromote-2ac0` | `2f97780` | 12 passed (`test_auto_promote_gate_policy` + `test_promotion_gates`) | `threshold_too_high` returns `held`; `auto_lower_threshold` / `retroactive_signal_update` strings gone from `hypothesis.py` | APPROVE |
| `cursor/ox-bind-loopback-2ac0` | `a1fbe37` | 3 passed | `start.bat` / `overnight_setup.py` have no `0.0.0.0`; default `127.0.0.1` via `CALLISTO_BIND_HOST` | APPROVE |
| `cursor/ox-loop-refresh-2ac0` | `1227f4a` | 19 passed | Default `_phase_refresh_signals` SELECT+return; writes only if `CALLISTO_ALLOW_SIGNAL_REFRESH=1`. Phase failures recorded. | APPROVE |
| `cursor/ox-eventloop-2ac0` | `f02c7f2` | 39 passed | `asyncio.to_thread` on sim / detect_regime / health-file; cache max 32; health timestamp updates only after successful write | APPROVE |

Hermes zombie: loop-refresh PID 18004 still waiting on the model after
OX_DONE+push. Interrupted via tmux Ctrl-C (not `pkill`). Slot refilled.
