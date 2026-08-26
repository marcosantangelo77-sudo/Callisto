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

`master` currently tracks `origin/master` and was last verified at
`2c1eaa1`. The current master checkout must stay clean while candidate
worktrees are reviewed.

## What has safely reached master

These reviewed commits have been pushed to `origin/master`:

| Commit | Change |
| --- | --- |
| `a181e9f` | Own event-bus lifecycle tasks correctly. |
| `1f70af2` | Retry transient POST 403s. |
| `2c1eaa1` | Validate duplicate fetch provenance records. |

Everything below is a candidate or WIP until independently approved.

## Live fleet state

Use at most **three direct Hermes/OX terminal workers at once**. This is a
deliberate host-reliability cap, not a known Nous concurrency limit: four
simultaneous terminal/test workloads previously led to external process-group
losses. Refill a slot immediately after a worker truly exits.

At the time this document was written, these three focused repair turns were
live:

| Priority | Branch / worktree | Goal |
| --- | --- | --- |
| 1 | `codex/checkpoint-trace-fidelity` at `/private/tmp/callisto-checkpoint-trace-fidelity` | Narrow worker: make resume use only validated admitted evidence and reject forged independent-source credit. Malformed outcome data is queued for a later turn. |
| 2 | `codex/run-persistence-unique-id` at `/private/tmp/callisto-run-persistence-unique-id` | Bind publication success to inode provenance and close post-rename/CLI race semantics. |
| 3 | `codex/db-writer-shutdown` at `/private/tmp/callisto-db-writer-shutdown` | No-edit OX adversarial review of the completed blocked-producer shutdown repair (`4609e04`). |

Workers are launched through:

```bash
bash /Users/marcosantangelo/callisto-wt/nous-supervisor.sh \
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
    '/private/tmp/callisto-checkpoint-trace-fidelity',
    '/private/tmp/callisto-run-persistence-unique-id',
    '/private/tmp/callisto-db-writer-shutdown',
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
| `codex/market-book-sanity` / `ae2cf32` | Shared market-book sanity gate across devig, consensus, scanner, placement/ranking, CLV, boost, and local devig. | New OX candidate. Independently retest invalid high-hold/NaN/crossed books, the live scanner-to-ranker path, `evaluate_percentage_boost(20, 100.9, .9)`, direct CLV contract, and tiny-hold power/shin normalization. Do not merge from the agent report alone. |
| `codex/stale-confidence-credit` / `fd496e6` | Expose genuine/stale counts and bounded stale penalty in calibration traces and WHY output. | **APPROVED** by independent adversarial review: 500 randomized supported-record cases preserved scoring/clamp math; trace/WHY/rehydration/no-stale controls passed. Merge pending. **Caution:** the worker briefly used prohibited checkout and stash actions on its own files, but review found the final diff clean and functionally correct. |

### Blocked candidates with replacement turns queued/running

| Branch / current head | Verified blocker | Correct next action |
| --- | --- | --- |
| `codex/checkpoint-trace-fidelity` / `9513ffc` | Resume path bypasses admission validation by consuming raw `payload['fetches']`; forged `independent_keys` can inflate confidence; blank rejected/skipped and malformed `rejections` fabricate honest-null/gate evidence. | Worker is active. Require one strict decoder used by all resume consumers, derived independent keys, and end-to-end adversarial tests. |
| `codex/run-persistence-unique-id` / `39e9ef0` | Normal post-rename success still trusts byte equality rather than inode identity; a moved final can duplicate records; cleanup can turn indeterminate publication into false durability; CLI lacks explicit do-not-retry semantics. | Worker is active. Require fd/inode validation and atomic foreign-replacement tests. |
| `codex/db-writer-shutdown` / `4609e04` | Earlier `6500300` queue-sweep candidate missed a producer blocked in `Queue.put()`: stop waited ~2 s despite a 50 ms timeout, raised incidental `AttributeError`, and stranded its future. | Replacement implementation `4609e04` is complete; an independent no-edit OX review is active. Require lifecycle synchronization and a deterministic blocked-producer test before any merge. |

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

1. Read this file, then inspect `master`, the candidate ledger, and the three
   active supervisor terminals/state rows.
2. Do not relaunch a duplicate worker until `ps` confirms the existing Hermes
   process is gone.
3. Wait for each worker’s summary, review its exact new head, and recycle its
   worker slot to the next highest-risk queued repair.
4. Merge approved `fd496e6` after the standard clean-master checks; continue
   adversarial review of `ae2cf32`. Update this file with every disposition
   and master SHA.
5. When implementation credits are exhausted, preserve this file and all
   pushed branches, stop launching new paid primary-agent work, and hand the
   same branch/task ledger to Cursor/Grok. The external OX workers may still
   be independently monitored by the same process/state-db method.

## Security and hygiene reminders

- Never include provider keys in a prompt, a commit, a test fixture, or this
  document.
- Do not expose the user’s global stash. Its current contents are user-owned.
- Use `apply_patch` for file edits and make each non-agent change auditable.
- Keep operator-facing status evidence-based: report process state, counters,
  diffs, focused tests, and review disposition—not optimistic model prose.
