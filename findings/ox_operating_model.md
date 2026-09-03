# OX operating model — Grok is expensive, OX is the loop

The 8-minute keep-six Grok timer burned usage. Replacement:

1. `scripts/ox-fleet.sh` (tmux session `ox-fleet`) keeps 6 `comm==hermes`
   workers, fed from `scripts/ox-queue.tsv` / `/tmp/ox_queue/queue.tsv`.
2. Each worker gets a **long** extract (god-module split), pushes
   `cursor/ox-<task>-2ac0`, writes `OX_DONE.md`, and stops.
3. The fleet SIGINT-s leftover Hermes on that worktree and launches the
   next free queue row. Exclusive files are claimed so two OX do not
   edit `api.py` at once.
4. **Nobody merges to master in that loop.** Grok (or Marco) batch-reviews
   a pile of branches later.

Do not resubscribe an 8-minute Grok timer.
