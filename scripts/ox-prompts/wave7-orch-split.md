# OX TASK: split orchestrator.py pipeline stages (LONG)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-orch-split-2ac0`
Worktree: `/tmp/callisto-ox-orch-split`

LONG extract. `orchestrator.py` is ~1957 lines. Move AGP pipeline helpers
(fetch/seal/world-query staging, not the class name) into `agp/pipeline_*.py`
or `tools/orch/`. Keep `Orchestrator` import path stable for `api.py`.

## Exclusive files (HARD)

You MAY edit:
- `orchestrator.py`
- `tools/orch/` or `agp/pipeline_support.py` (create)
- `tests/test_orch_split.py` (create)

Do NOT weaken seals. Do NOT invent an unkeyed verify path.
Do NOT touch `api.py` except if a type-check import must stay.
Do NOT arm betting.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. No merge to master.

## Required

- `from orchestrator import Orchestrator` still works.
- Shrink orchestrator.py by moving real functions (hundreds of lines), not
  comments. Facade is OK.
- Source pin: verify/seal call sites still go through keyed helpers.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_orch_split.py tests/test_agp_seal.py tests/test_preregistration_seal.py -q
```

Skip missing. Commit: `refactor(orch): extract pipeline helpers out of orchestrator.py`

Write `OX_DONE.md` with line counts.
