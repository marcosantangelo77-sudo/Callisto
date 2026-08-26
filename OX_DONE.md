# OX_DONE — gate remaining research/odds dump GETs

Branch: `cursor/ox-remaining-get-2ac0`
Commit: `5fd609f` — `fix(api): gate remaining edges/session/world/debug GETs`
Pushed to origin.

## Audit of the six reported paths

- `/odds/edges` — **newly gated** via decorator
- `/edges/live` — **newly gated** via `_auth` signature param (multi-line decorator; test uses a source pin on the function def instead)
- `/session/{session_id}` — already gated (`_auth: None = Depends(require_admin_or_loopback)`); skipped
- `/world/{domain}` — already gated (same pattern); skipped
- `/debug/memory` — already gated with `require_admin` (stricter); left as-is
- `/debug/memory/top-traces` — already gated with `require_admin`; left as-is
- `/odds/opportunities` — **newly gated** (one-line match to `/odds/edges`, permitted by task)

## Exact changes

api.py:
- `@app.get("/odds/edges", dependencies=[Depends(require_admin_or_loopback)])`
- `@app.get("/odds/opportunities", dependencies=[Depends(require_admin_or_loopback)])`
- `get_live_edges`: added `_auth: None = Depends(require_admin_or_loopback)` parameter

tests/test_sensitive_get_gating.py:
- SENSITIVE_GETS += `/odds/edges`, `/odds/opportunities`
- New `test_route_gated_via_signature_auth_param` source pin for `get_live_edges` requiring `require_admin_or_loopback` in the def

## Test output

```
$ /tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py -q
18 passed, 1 warning in 0.44s
```
