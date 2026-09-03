# OX TASK (WAVE 2 — queued, do not start until orchestrator launches you)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in the worktree
the supervisor passed via WORKING DIRECTORY.

## Exclusive file ownership (HARD)

You MAY edit:
- `start.bat`
- `scripts/overnight_setup.py`
- `tests/test_bind_host_launchers.py` (create)

You MUST NOT edit `api.py` (it already defaults `CALLISTO_BIND_HOST` to `127.0.0.1`).
You MUST NOT edit other files, credentials, `config/providers.yaml`, or `master`.

## Git rules (HARD)

- Stay on the branch the worktree was created with.
- No `git stash`, `git reset --hard`, `git checkout --`.
- No merge. After tests, commit and `git push -u origin HEAD`.

## Bug (verified)

`api.py` defaults bind to `127.0.0.1`, but launchers override it:

- `start.bat` ~85–87: `uvicorn api:app --host 0.0.0.0 --port 8420`
- `scripts/overnight_setup.py` ~59: `'--host', '0.0.0.0'`

That publishes the API (and ungated GETs) on every interface.

## Required change

Both launchers must bind `127.0.0.1` by default.

- Read host from env `CALLISTO_BIND_HOST` if set, else `127.0.0.1`.
- `start.bat`: something like
  `if not defined CALLISTO_BIND_HOST set CALLISTO_BIND_HOST=127.0.0.1`
  then `--host %CALLISTO_BIND_HOST%`.
- `overnight_setup.py`: `os.environ.get("CALLISTO_BIND_HOST", "127.0.0.1")`.
- Do not hardcode `0.0.0.0` anywhere in these two files after the change.

## Tests

`tests/test_bind_host_launchers.py`:
- Read `start.bat` and `scripts/overnight_setup.py` as text.
- Assert `0.0.0.0` is absent.
- Assert `127.0.0.1` and/or `CALLISTO_BIND_HOST` are present.
- For the Python file, exec or import the host expression if easy; source
  inspection is enough if import would start uvicorn.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_bind_host_launchers.py -q
```

Commit: `fix(launch): default API bind to loopback via CALLISTO_BIND_HOST`

Write `OX_DONE.md` with SHA and test output.
