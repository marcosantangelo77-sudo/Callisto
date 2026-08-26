# OX Alpha dispatch — 2026-08-26

Operator: spawn OX workers for the audit criticals, then (orchestrator)
write the production-ready / shipping-model note. Host cap remains **3**
concurrent Hermes processes (workstation previously lost process groups at
4; this VM is 16 GB). Portal is free; the cap is the host, not the bill.
Refill a slot as soon as a worker truly exits.

Orchestrator does not implement these diffs. Independent review before merge.

## Wave 1 — LIVE (2026-08-26)

| Slot | Task | Branch | Worktree | Exclusive files | Prompt |
| --- | --- | --- | --- | --- | --- |
| 1 | Gate `_phase_refresh_signals`; record phase failures | `cursor/ox-loop-refresh-2ac0` | `/tmp/callisto-ox-loop-refresh` | `tools/autonomous.py` + two new tests | `scripts/ox-prompts/wave1-loop-refresh.md` |
| 2 | `auto_promote` diagnose-only | `cursor/ox-autopromote-2ac0` | `/tmp/callisto-ox-autopromote` | `tools/hypothesis.py` + one new test | `scripts/ox-prompts/wave1-autopromote.md` |
| 3 | Event-loop offload | `cursor/ox-eventloop-2ac0` | `/tmp/callisto-ox-eventloop` | `api.py` + one new test | `scripts/ox-prompts/wave1-eventloop.md` |

Launch:

```bash
export PATH="$HOME/.local/bin:$HOME/.hermes/bin:$PATH"
# PTY via tmux; do not nohup
bash /workspace/scripts/nous-supervisor.sh <task> <worktree> <prompt> 180
```

Model: `stealth/ox-alpha` / provider `nous`. Supervisor refuses `master`.

Hermes PIDs at launch (2026-08-26 ~03:57Z): loop-refresh `18004`,
autopromote `18276`, eventloop `18444`. Tmux: `ox-loop-refresh`,
`ox-autopromote`, `ox-eventloop`. Re-check with `pgrep -af hermes`
before treating a slot as free. Logs: `/workspace/logs/oxa/<task>.log`.

## Wave 2 — QUEUED (launch when a wave-1 slot frees)

| Next | Task | Suggested branch | Exclusive files | Prompt |
| --- | --- | --- | --- | --- |
| 4 | Loopback bind in launchers | `cursor/ox-bind-loopback-2ac0` | `start.bat`, `scripts/overnight_setup.py` | `scripts/ox-prompts/wave2-bind-loopback.md` |
| 5 | Telegram `/resume_all` + OrderManager default off | `cursor/ox-telegram-arming-2ac0` | `tools/telegram_bot.py`, `tools/order_manager.py` | `scripts/ox-prompts/wave2-telegram-arming.md` |
| 6 | Keyed seal fail-closed | `cursor/ox-seal-fail-closed-2ac0` | `agp/__init__.py`, `tests/test_tier3_epi_seal.py` | `scripts/ox-prompts/wave2-seal-fail-closed.md` |

Do not start wave 2 until a Hermes slot is free. Do not raise the host cap
without evidence the VM stays stable.

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
