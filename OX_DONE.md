# OX DONE: split api.py wiki+analysis handlers into tools.api

Commit: `refactor(api): extract wiki/analysis handlers to tools.api`

## Routes moved (thin wrapper in api.py, body in tools/api/)

### tools/api/wiki.py
- GET /wiki/stats
- GET /wiki/articles
- GET /wiki/article/{topic}
- GET /wiki/search
- GET /wiki/contradictions

### tools/api/analysis.py
- GET /analysis/futures-efficiency
- GET /analysis/half-market/{sport}
- GET /analysis/cross-tabulate/{sport}

### tools/api/odds_extra.py
- GET /odds/psychology/{sport}
- GET /odds/psychology
- GET /odds/dead-numbers/{sport}
- GET /odds/line-analysis/{sport}

## Invariants kept

- All `@app.get(...)` decorators remain in api.py with
  `dependencies=[Depends(require_admin_or_loopback)]` unchanged.
- No auth semantics changed; no endpoints gated or ungated.
- Public handler function names preserved; wrappers delegate to the module.

## Tests

- `tests/test_api_split_wiki.py` created (source pins: moved strings live in
  tools/api modules, decorators still carry require_admin_or_loopback).
- `tests/test_sensitive_get_gating.py` passes unmodified.

`/tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py tests/test_api_split_wiki.py -q`
=> 52 passed.
