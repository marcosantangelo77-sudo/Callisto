# OX TASK: split inference.py without unifying routers (LONG)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-inference-split-2ac0`
Worktree: `/tmp/callisto-ox-inference-split`

LONG extract. `inference.py` is ~1788 lines with TWO planes. Split files;
do NOT point MODEL_LADDER at ProviderRouter. Measured Hermes latency is
p50 ≈ 11.9s max ≈ 31.4s (`findings/hermes_latency_2026-08-26.md`).

## Exclusive files (HARD)

You MAY edit:
- `inference.py` (facade re-exports)
- `inference_kernel.py` and/or `inference_router.py` (create)
- `tests/test_inference_planes.py`
- `tests/test_inference_split.py` (create)

Do NOT delete either plane. Do NOT add hermes CLI to the kernel ladder.
Do NOT import `tools.autonomous`.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. No merge to master.

## Required

- `import inference` still provides `MODEL_LADDER`, `complete`, `ProviderRouter`,
  `load_providers_config`.
- Kernel walk (`complete` / MODEL_LADDER) lives in one module; ProviderRouter
  in another; `inference.py` re-exports.
- tests/test_inference_planes.py still pass; comment still forbids unify.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_inference_planes.py tests/test_inference_split.py tests/test_hermes_latency_script.py -q
```

Commit: `refactor(inference): split kernel ladder and ProviderRouter modules`

Write `OX_DONE.md`.
