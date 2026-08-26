# OX concurrency probe — is 3 the max?

**Date:** 2026-08-26 ~04:03–04:05Z
**Host:** this cloud VM, 16 GiB RAM, 4 CPUs, no swap.
**Question:** the handoff cap of 3 was workstation lore (4 simultaneous
terminal/test workloads previously killed process groups). Operator asked
to test 6, matching an earlier successful run.

## Method

1. Snapshot T0 with 3 live Hermes workers (wave 1).
2. Set `CALLISTO_HERMES_MAX_PROCS=6` (supervisor default is still 3).
3. Launch wave 2 (bind, telegram, seal) in tmux via `nous-supervisor.sh`,
   waiting for the Hermes PID count to rise between launches so the
   preflight cap does not race.
4. Re-measure at T+90s. Hunt tmux panes for `429` / rate-limit / OOM.

Exclusive files remained disjoint. No `pkill`.

## Result

**Six concurrent `stealth/ox-alpha` Hermes processes spawned. Nous Portal
did not 429 in this window. This VM did not OOM.** Three is not a Portal
limit.

| Time (UTC) | Hermes PIDs | pyright LSP | RAM used / avail | Notes |
| --- | --- | --- | --- | --- |
| 04:02:59 T0 | 3 (`18004,18276,18444`) | 5 | 2.1 / 13 GiB | wave 1 only |
| 04:03:21 T1 | 6 (`18004,18276,18444,23968,24269,24575`) | 4 | 2.3 / 13 GiB | all original PIDs still alive |
| 04:04:52 T2 | 6 (churn; see below) | 5 | 2.2 / 13 GiB | load average 0.11; no OOM |

PID deaths in the window were **task completion**, not kills:

- `18276` autopromote — exited after `2f97780` push. Shell prompt back.
- `23968` bind-loopback — exited after `a1fbe37` push (~90s, small task).

No `429`, `rate limit`, `quota`, or `overload` strings in any of the six
tmux panes. `dmesg` showed no OOM.

Local cost of a worker is roughly **150 MB Hermes Python + 0–1 pyright
(~250–330 MB)**. Six of those fit easily in 16 GiB. The workstation
failure mode (process-group loss at 4) was a different machine doing
terminals + tests together, not this VM and not Portal.

## Policy after the probe

| Machine | Cap | How |
| --- | --- | --- |
| This 16 GiB cloud VM | **6** is proven | `CALLISTO_HERMES_MAX_PROCS=6` |
| Operator workstation | **3** until re-probed there | default in `nous-supervisor.sh` stays 3 |

Do not raise the **default** in the supervisor to 6. That default exists
to protect the workstation. Raise per-host with the env var.

Candidates produced during the probe are still **unreviewed**:
`cursor/ox-autopromote-2ac0` @ `2f97780`,
`cursor/ox-bind-loopback-2ac0` @ `a1fbe37`. Independent review before merge.

## Still running at 04:05Z

`ox-loop-refresh`, `ox-eventloop`, `ox-telegram-arming`, `ox-seal-fail-closed`.
Refill a slot to 6 only when there is a disjoint-file task ready.
