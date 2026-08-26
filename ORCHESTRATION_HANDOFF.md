# Callisto orchestration handoff

This is a live handoff for the next orchestrator (for example Cursor + Grok).
It records verified state, not agent self-reports. Update it after every
review, merge, worker replacement, or provider incident.

## Mission and operating boundary

The operator asked for a high-throughput but safe Callisto improvement loop:

1. Use free **Nous Portal / OX Alpha** workers for implementation.
2. Keep the primary orchestrator focused on dispatch, liveness, adversarial
   review, checkpointing, and reviewed merges—not on doing every coding task.
3. Never merge a candidate merely because its worker says tests passed.
4. Keep the user’s work and credentials safe. Do not write secrets into this
   file, prompts, commits, logs, or chat responses.

The checkout is:

```text
/Users/marcosantangelo/Documents/ChatGPT/callisto
```

At the final freeze snapshot, `master` was clean and tracking
`origin/master`. The last reviewed code change is `4c79807`; later master
commits before this snapshot are handoff documentation only. The current
master checkout must stay clean while candidate worktrees are reviewed.

## What has safely reached master

These reviewed commits have been pushed to `origin/master`:

| Commit | Change |
| --- | --- |
| `a181e9f` | Own event-bus lifecycle tasks correctly. |
| `1f70af2` | Retry transient POST 403s. |
| `2c1eaa1` | Validate duplicate fetch provenance records. |
| `47ae16f` | Keep stale evidence from earning confidence credit and make stale penalties/counts explicit in calibration and WHY diagnostics. |
| `4c79807` | Settle DB-writer producers blocked at queue admission during forced shutdown. |

Everything below is a candidate or WIP until independently approved.

## Live fleet state

Use at most **three direct Hermes/OX terminal workers at once**. This is a
deliberate host-reliability cap, not a known Nous concurrency limit: four
simultaneous terminal/test workloads previously led to external process-group
losses. Portal is free; do not raise the host cap just because OX is free.
Outside an explicit operator freeze, refill a slot immediately after a
worker truly exits.

Operator freeze is **lifted** (2026-08-26): spawn OX for audit criticals.
Product-direction work (`findings/production_ready_2026-08-26.md`) is
orchestrator-owned; do not block it on OX.

### Active OX wave 1 (cloud VM, 2026-08-26)

| Slot | Branch / worktree | Goal |
| --- | --- | --- |
| 1 | `cursor/ox-loop-refresh-2ac0` at `/tmp/callisto-ox-loop-refresh` | Gate `_phase_refresh_signals` (default no writes); record `_loop` phase failures. Owns `tools/autonomous.py` only. |
| 2 | `cursor/ox-autopromote-2ac0` at `/tmp/callisto-ox-autopromote` | `auto_promote` diagnose-only: no `edge_threshold` / `signal_generated` rewrites. Owns `tools/hypothesis.py` only. |
| 3 | `cursor/ox-eventloop-2ac0` at `/tmp/callisto-ox-eventloop` | `asyncio.to_thread` for portfolio sim + `detect_regime`; debounce health-file IO; bound sim cache. Owns `api.py` only. |

Prompts: `scripts/ox-prompts/wave1-*.md`. Tmux sessions: `ox-loop-refresh`,
`ox-autopromote`, `ox-eventloop`. Supervisor:
`bash scripts/nous-supervisor.sh` (in-tree; PR #28 on
`cursor/ox-alpha-nous-portal-2ac0` if not yet on master).

### Wave 2 queued (launch when a slot frees)

| Next | Prompt | Exclusive files |
| --- | --- | --- |
| bind | `scripts/ox-prompts/wave2-bind-loopback.md` | `start.bat`, `scripts/overnight_setup.py` |
| telegram | `scripts/ox-prompts/wave2-telegram-arming.md` | `tools/telegram_bot.py`, `tools/order_manager.py` |
| seal | `scripts/ox-prompts/wave2-seal-fail-closed.md` | `agp/__init__.py`, `tests/test_tier3_epi_seal.py` |

### Frozen unreviewed candidates — do not merge yet

| Branch / head | Status |
| --- | --- |
| `codex/checkpoint-trace-fidelity` / `dbcc751` | Unreviewed. Independent adversarial review still required. |
| `codex/run-persistence-unique-id` / `1ec9778` | Unreviewed. Independent adversarial review still required. |
| `codex/db-writer-shutdown` | Already squash-merged as `4c79807`. |

Prepared but still not launched (old freeze leftovers):

```text
/private/tmp/ox_market_raw_input_placement_repair_prompt.md
/private/tmp/ox_checkpoint_trace_outcome_rejection_shape_repair_prompt.md
```

Workers are launched through the in-repo supervisor (workstation copy at
`~/callisto-wt/nous-supervisor.sh` is the same contract):

```bash
bash scripts/nous-supervisor.sh \
  <task-name> <worktree> <prompt-file> 180
```

Launch in a persistent foreground PTY and retain the returned terminal session
identifier. Do **not** use `nohup` or background the supervisor yourself.
The supervisor itself starts Hermes with:

```text
--provider nous -m stealth/ox-alpha
```

Avoid OpenRouter/OpenCode unless the operator explicitly changes that policy.
Do not record or reuse API keys in the handoff.

### Fleet-health test

A quiet worktree is not proof of a zombie, and `ended_at IS NULL` alone is
not proof of life. Require both a living OS process and advancing persisted
Hermes counters/messages:

```bash
python3 - <<'PY'
import os, sqlite3
con = sqlite3.connect(os.path.expanduser('~/.hermes/state.db'))
for cwd in [
    '/tmp/callisto-ox-loop-refresh',
    '/tmp/callisto-ox-autopromote',
    '/tmp/callisto-ox-eventloop',
]:
    print(cwd, con.execute(
        'SELECT id, ended_at, api_call_count, input_tokens, output_tokens, '
        'tool_call_count, message_count, last_activity_description '
        'FROM sessions WHERE cwd=? ORDER BY started_at DESC LIMIT 1',
        (cwd,),
    ).fetchone())
PY
```

Then correlate with the exact process, worktree diff, and supervisor activity
log. A provider message such as “waiting for stream response” or a recoverable
API retry is not automatically a rate limit: real counter movement afterward
means the turn recovered. If counters stop for the supervisor’s idle window,
let the supervisor terminate/reclassify the worker. If an agent launches an
unbounded test against instructions, identify the exact child PID first and
only then interrupt that child; never use `pkill`, `killall`, or broad process
matching.

## Candidate ledger and review status

### Ready for independent review

| Branch / head | Scope | Review status |
| --- | --- | --- |
| `codex/market-book-sanity` / `ae2cf32` | Shared market-book sanity gate across devig, consensus, scanner, placement/ranking, CLV, boost, and local devig. | **BLOCKED** by review: percentage boost still turns `100.9` into `SLAM`; scanner accepts fractional raw odds; present malformed pair values fall back to a one-sided prior; public consensus primitives bypass the gate; invalid-placement skip persistence fails. The narrow raw-input/placement repair prompt exists above but must not be launched during the freeze. |

### Unreviewed completed candidates — frozen, do not merge yet

| Branch / current head | Predecessor finding / unreviewed scope | Correct next action |
| --- | --- | --- |
| `codex/checkpoint-trace-fidelity` / `dbcc751` | The prior `9513ffc` resumed from raw `payload['fetches']` and trusted serialized `independent_keys`, permitting confidence laundering. The new worker reports one strict validated-admitted-fetch decoder shared by fetch-cache, resume hydration, and ledger replay; it derives independence keys instead of trusting payload keys. It intentionally does **not** repair blank `rejected`/`skipped` values or malformed top-level `rejections`; legacy checkpoints without admission markers are conservatively refetched/rejected as source evidence. | Completed and pushed, but **unreviewed/unmerged under the freeze**. Independently exercise forged keys, raw/unadmitted fetches, resume/replay/cache-hit consumers, malformed outcome/rejection shapes, and legacy compatibility before any merge. |
| `codex/run-persistence-unique-id` / `1ec9778` | Earlier `39e9ef0` trusted byte equality rather than inode identity; a moved final could duplicate records; cleanup could turn indeterminate publication into false durability; CLI lacked explicit do-not-retry semantics. | Replacement completed and pushed, but is **unreviewed/unmerged under the freeze**. It claims fd/inode-bound post-commit verification, guarded retry, indeterminate cleanup propagation, nonzero explicit CLI warning, and atomic-swap regressions. Cursor must independently reproduce all prior race cases before merging. |

### Parked: requires a deployment-policy decision before merge

| Branch / head | Why parked |
| --- | --- |
| `codex/claim-journal-tail-seal` / `399fb44` | The candidate added an explicit seal-policy regime but still silently re-chains a broken signed journal during migration, leaves `migrated_unverified` unsealed, blindly appends to legacy/corrupt history, permits cross-claim replay, leaks raw key prefixes, and would fail closed in current deployments without a runtime `CALLISTO_SEAL_POLICY` decision. Do **not** merge or dispatch another broad repair until the operator decides deployment migration policy. |

## Review and merge protocol

For every completed worker:

1. Confirm `git status --short --branch`, worker exit, commit SHA, and push.
2. Spawn an independent reviewer against that exact worktree and SHA. The
   reviewer must not edit it, must run only focused tests, and must attempt
   adversarial repros based on the task’s claimed invariants.
3. If blocked, write a narrow follow-up OX prompt and recycle a worker slot.
   Preserve source work with a WIP checkpoint commit before replacing an agent.
4. Only after an explicit independent **APPROVE**, squash merge into a clean
   master checkout:

```bash
git status --short --branch
git fetch origin master
git rev-list --left-right --count master...origin/master
git merge --squash <approved-branch>
git diff --check
# Run the reviewer-approved focused tests here.
git commit -m "<conventional reviewed change>"
git push origin master
git status --short --branch
```

Do not merge WIP checkpoints or candidates with an unresolved reviewer BLOCK.

## What worked in this loop

- **Dedicated worktrees + branch ownership:** each OX worker gets exactly one
  branch and cannot collide with master or another worker.
- **Persistent supervised Nous turns:** actual Hermes state-db counters made
  it possible to distinguish a slow model response from a zombie process.
- **Three focused workers, immediately recycled:** better sustained throughput
  than allowing host instability to kill a larger fleet.
- **Independent adversarial review:** this found real false-success and
  safety failures that broad/happy-path OX test suites missed, including
  persistence provenance races, checkpoint confidence laundering, and
  blocked DB producers.
- **Focused prompts with explicit invariants:** “add tests” was insufficient;
  exact adversarial repros and prohibited actions made later turns materially
  better.
- **WIP checkpoints before interruption:** no worker work was silently lost.

## What did not work / guardrails to preserve

- Do not infer success from a clean/full test run; tests often missed the
  adversarial path that mattered.
- Do not use a broad `pytest tests/` sweep as a default. One Claim worker
  repeated unscoped whole-suite tests and was stopped; a focused test can also
  hang if it lacks an in-test bound.
- Do not let OX alter provider configuration, credentials, the user’s stash,
  master, or another worktree.
- Do not permit `git stash`, `git reset --hard`, or `git checkout --` during
  worker tasks. Preserve the user stash; it is not an implementation buffer.
- Treat database session records as evidence, not authority: an interrupted
  Hermes process can leave `ended_at=NULL` behind.
- Provider pauses/errors happened, but there was no demonstrated systemic
  Nous rate-limit/capacity rejection during the active successful turns.
- The original broad security Claim task grew too large and crosses deployment
  policy. Park ambiguous policy changes rather than “fixing” them by guesswork.

## First actions for the successor

1. Check OX liveness: `python3 scripts/oxa_status.py` (exit 0), then the
   fleet-health sqlite snippet above plus `pgrep -af hermes` and tmux
   sessions `ox-loop-refresh`, `ox-autopromote`, `ox-eventloop`.
2. When a wave-1 worker **exits**, independently review its SHA (focused
   tests + adversarial repros). Do not merge on OX testimony. Recycle the
   slot onto the next wave-2 prompt.
3. Do not merge `dbcc751` / `1ec9778` until that independent review is done.
   Keep market `ae2cf32` blocked and Claim `399fb44` parked.
4. Product direction is in `findings/production_ready_2026-08-26.md`. Do not
   start a website/SaaS effort until Stage A (fail-closed) lands.
5. Preserve this file and all pushed branches when changing orchestrators.

## Security and hygiene reminders

- Never include provider keys in a prompt, a commit, a test fixture, or this
  document.
- Do not expose the user’s global stash. Its current contents are user-owned.
- Use `apply_patch` for file edits and make each non-agent change auditable.
- Keep operator-facing status evidence-based: report process state, counters,
  diffs, focused tests, and review disposition—not optimistic model prose.
