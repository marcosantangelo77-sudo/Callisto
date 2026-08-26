# OX Alpha dispatch — 2026-08-26

Operator: spawn OX workers for the audit criticals, then (orchestrator)
write the production-ready / shipping-model note.

**Concurrency (probed):** this 16 GiB cloud VM ran **6** concurrent
Hermes/OX workers with no Portal 429 and no OOM. Launch with
`CALLISTO_HERMES_MAX_PROCS=6`. The supervisor **default stays 3** for the
workstation. See `findings/ox_concurrency_probe_2026-08-26.md`.

Orchestrator does not implement these diffs. Independent review before merge.

**Superseded for fleet state:** `findings/ox_wave4_fleet.md` and
`findings/scorecard_50.md`. Master is past Stage A (`373352e`).

## Wave 1

| Slot | Task | Branch | Worktree | State |
| --- | --- | --- | --- | --- |
| 1 | Gate `_phase_refresh_signals`; record phase failures | `cursor/ox-loop-refresh-2ac0` | `/tmp/callisto-ox-loop-refresh` | LIVE |
| 2 | `auto_promote` diagnose-only | `cursor/ox-autopromote-2ac0` | `/tmp/callisto-ox-autopromote` | done `2f97780` — unreviewed |
| 3 | Event-loop offload | `cursor/ox-eventloop-2ac0` | `/tmp/callisto-ox-eventloop` | LIVE |

## Wave 2 (launched during 6-wide probe)

| Slot | Task | Branch | Worktree | State |
| --- | --- | --- | --- | --- |
| 4 | Loopback bind | `cursor/ox-bind-loopback-2ac0` | `/tmp/callisto-ox-bind-loopback` | done `a1fbe37` — unreviewed |
| 5 | Telegram `/resume_all` + OrderManager default off | `cursor/ox-telegram-arming-2ac0` | `/tmp/callisto-ox-telegram-arming` | LIVE |
| 6 | Keyed seal fail-closed | `cursor/ox-seal-fail-closed-2ac0` | `/tmp/callisto-ox-seal-fail-closed` | LIVE |

Launch:

```bash
export PATH="$HOME/.local/bin:$HOME/.hermes/bin:$PATH"
export CALLISTO_HERMES_MAX_PROCS=6   # this VM only
bash /workspace/scripts/nous-supervisor.sh <task> <worktree> <prompt> 180
```

Model: `stealth/ox-alpha` / provider `nous`. Supervisor refuses `master`.
Logs: `/workspace/logs/oxa/<task>.log`.

## Explicit non-goals

- Do not widen `generate_paper_trade_signal` to `status=='live'`.
- Do not merge `codex/checkpoint-trace-fidelity` (`dbcc751`) or
  `codex/run-persistence-unique-id` (`1ec9778`) on worker testimony.
- Do not implement the website/SaaS product. See
  `findings/production_ready_2026-08-26.md`.
- Do not split `autonomous.py` this wave.

## Review protocol

For each finished worker: confirm process exit + SHA + push; spawn an
independent reviewer (not OX self-report); focused tests + adversarial
repros; APPROVE then squash to a clean master. BLOCK → narrow follow-up
prompt, recycle the slot.
