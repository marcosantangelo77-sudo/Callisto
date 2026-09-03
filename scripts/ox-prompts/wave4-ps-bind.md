# OX TASK: PowerShell launchers must default to loopback

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-ps-bind-2ac0`
Worktree: `/tmp/callisto-ox-ps-bind`

## Exclusive files (HARD)

You MAY edit:
- `scripts/watchdog.ps1`
- `scripts/start-callisto.ps1`
- `tests/test_bind_host_ps_launchers.py` (create — do NOT edit
  `tests/test_bind_host_launchers.py`, that file belongs to another branch)

You MUST NOT edit `start.bat`, `scripts/overnight_setup.py`, `api.py`,
credentials, or `master`. Do not change searxng bind (local search engine).

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.

## Bug (verified)

`start.bat` / `overnight_setup.py` were already switched to
`CALLISTO_BIND_HOST` default `127.0.0.1`. These two Windows launchers still
pass `--host 0.0.0.0` to uvicorn, which is the original LAN-exposure hole.

## Required change

Both scripts: uvicorn `--host` must use `CALLISTO_BIND_HOST` and default to
`127.0.0.1` when the env var is unset. Match the bat file's behavior.

Do not add a silent fallback to `0.0.0.0`.

## Tests

`tests/test_bind_host_ps_launchers.py`:
- Neither ps1 contains the literal `0.0.0.0` as a uvicorn host
  (comment mentioning the old hole is OK if you must; prefer no `0.0.0.0` at all).
- Both mention `CALLISTO_BIND_HOST` and `127.0.0.1`.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_bind_host_ps_launchers.py -q
```

Commit: `fix(launch): PowerShell uvicorn bind defaults to loopback`

Write `OX_DONE.md` with SHA and test output.
