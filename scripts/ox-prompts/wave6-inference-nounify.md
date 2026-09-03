# OX TASK: record measured Hermes latency; do NOT unify MODEL_LADDER

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-inference-nounify-2ac0`
Worktree: `/tmp/callisto-ox-inference-nounify`

## Exclusive files (HARD)

You MAY edit:
- `inference.py` (the TWO INFERENCE PLANES comment block only, plus a
  one-line pointer if needed)
- `tests/test_inference_planes.py`
- `findings/hermes_latency_2026-08-26.md` (comment cross-link only; do not
  rewrite the measurements)

Do NOT point `MODEL_LADDER` / `inference.complete()` at `ProviderRouter`.
Do NOT delete either plane. Do NOT add a hermes CLI subprocess to the
kernel ladder. Do NOT call `scripts/measure_hermes_latency.py` (numbers
already exist). Do NOT import `tools.autonomous`.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. Base origin/master.

## Why

Stage C "one inference plane" is blocked by measurement: p50 ≈ 11.9s,
max ≈ 31.4s (`findings/hermes_latency_2026-08-26.md`). The existing
comment still says "~14s historically" and "a later PR may point the
kernel at it". Update the pin so nobody unifies on a sub-10s assumption.

## Required

1. In the TWO INFERENCE PLANES comment, cite the measured p50/max and
   state explicitly that unifying this wave is forbidden.
2. Tests:
   - both planes still exist (keep existing tests).
   - `inference.py` source contains `p50` / `11.9` or the findings
     filename (so deleting the measurement silently fails).
   - `inference.complete` / MODEL_LADDER walk does not mention
     `hermes_cli` as a kernel transport (source pin on the ladder dict /
     complete() — ProviderRouter may still mention hermes_cli).

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_inference_planes.py tests/test_hermes_latency_script.py -q
```

Commit: `docs(inference): do not unify MODEL_LADDER; Hermes p50 is 12s`

Write `OX_DONE.md`.
