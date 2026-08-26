# OX TASK: measure Hermes complete() latency — do not unify routers

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-hermes-latency-2ac0`
Worktree: `/tmp/callisto-ox-hermes-latency`

## Exclusive files (HARD)

You MAY edit:
- `scripts/measure_hermes_latency.py` (create)
- `tests/test_hermes_latency_script.py` (create — argparse/help only, no live call)
- `findings/hermes_latency_2026-08-26.md` (create with whatever you measured)

Do NOT change `inference.py` MODEL_LADDER. Do NOT point the kernel at
Hermes. Do NOT edit `tools/autonomous.py`. This is a measurement PR.

## Git rules

No full suite. Push.

## Goal

Stage C (one inference plane) is blocked on unknown `hermes -z` fork
latency (~14s historically). Measure it on this VM.

`scripts/measure_hermes_latency.py`:
- Invokes the same binary the supervisor uses (`hermes --provider nous -m stealth/ox-alpha`)
  with a tiny `-z PONG` (or equivalent) and `--in` a temp dir.
- Prints elapsed_ms, exit code, and whether stdout contains PONG.
- Timeout 60s. Does not print tokens/auth.
- If hermes/auth missing, exit 2 with a one-line reason.

Run it once, capture numbers into `findings/hermes_latency_2026-08-26.md`
(n=3 if cheap, n=1 if slow). Include: p50/max if n>1, hermes path, model.

Tests: script exists, `--help` works, does not import `tools.autonomous`.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_hermes_latency_script.py -q
```

Commit: `chore: measure Hermes CLI latency before unifying MODEL_LADDER`

Write `OX_DONE.md` with the measured ms.
