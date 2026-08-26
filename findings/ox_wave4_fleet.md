# OX fleet — live (2026-08-26 ~04:43Z)

Master: `db77c68`. Scorecard **≥50** (conservative ~55, optimistic ~67).

`CALLISTO_HERMES_MAX_PROCS=6` real Hermes. Supervisor pgrep inflates;
launch with 12 when refilling, keep **6** `comm==hermes` processes.

## Live exclusive ownership

| Task | Branch | Worktree | Owns |
| --- | --- | --- | --- |
| odds GET batch 2 | `cursor/ox-odds-get-batch2-2ac0` | `/tmp/callisto-ox-odds-get-batch2` | `api.py` |
| loop sequencer slice 2 | `cursor/ox-loop-sequencer-2ac0` | `/tmp/callisto-ox-loop-sequencer` | `tools/autonomous.py`, `tools/loop/sequencer.py` |
| compute_stake Kelly imports | `cursor/ox-compute-stake-kelly-2ac0` | `/tmp/callisto-ox-compute-stake-kelly` | `tools/bet_executor.py` imports |
| registry v2 | `cursor/ox-registry-v2-2ac0` | `/tmp/callisto-ox-registry-v2` | `tests/test_fail_closed_registry.py` |
| CLI help money | `cursor/ox-cli-help-2ac0` | `/tmp/callisto-ox-cli-help` | `callisto.py` |
| dashboard module copy | `cursor/ox-dashboard-module-2ac0` | `/tmp/callisto-ox-dashboard-module` | `tools/dashboard.py` |

## On finish

Independent focused tests → cherry-pick **fix SHA only** (strip `OX_DONE.md`)
→ `git push origin master`. SIGINT zombie Hermes PIDs only. Refill from
this file. Never add `"live"` to paper-signal statuses. Never full
`pytest tests/`.
