# OX TASK: document the two inference planes; do not merge them

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-inference-planes-2ac0`
Worktree: `/tmp/callisto-ox-inference-planes`

## Exclusive files (HARD)

You MAY edit:
- `inference.py` (module docstring / comments around MODEL_LADDER only)
- `tests/test_inference_planes.py` (create)

You MUST NOT change MODEL_LADDER contents, routing behavior, providers.yaml,
`tools/autonomous.py`, credentials, or `master`. Do not point production
code at Hermes CLI. Do not "unify" the routers in this PR.

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.

## Bug (verified)

Two inference planes:

- Kernel `inference.py` `MODEL_LADDER` (task_type → model list)
- `ProviderRouter` / `providers.yaml` (used by the CLI / pipeline)

A later PR may point the kernel at ProviderRouter **after measuring**
Hermes CLI fork latency (~14s historically). This PR only makes the
duplication visible and test-pinned so nobody "fixes" it by deleting
one ladder silently.

## Required change

1. Above `MODEL_LADDER`, a short comment: canonical *future* routing is
   ProviderRouter; MODEL_LADDER is still live for kernel `complete()`;
   do not delete either in drive-by refactors.
2. Tests: `MODEL_LADDER` has expected keys (`reasoning` at minimum);
   `load_providers_config` (or providers.yaml/json in-repo) exists and
   has at least one provider. No network. No latency measurement required.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_inference_planes.py -q
```

Commit: `docs(inference): pin dual MODEL_LADDER vs ProviderRouter planes`

Write `OX_DONE.md`.
