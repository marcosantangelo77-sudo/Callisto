# OX_DONE

Commit: `fix(api): gate remaining odds dump GETs (batch 2)`

Files changed:
- `api.py` — added `dependencies=[Depends(require_admin_or_loopback)]` to:
  - `GET /odds/movements` (line ~1525)
  - `GET /odds/snapshots/{sport}` (line ~1539)
  - `GET /odds/narrative-edges` (line ~1657)
  - `GET /odds/kl-metrics` (line ~1666)
  - `GET /odds/status` (line ~1943)
- `tests/test_sensitive_get_gating.py` — extended `SENSITIVE_GETS` with the five newly gated paths.

Tests: `/tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py -q` → 23 passed.
