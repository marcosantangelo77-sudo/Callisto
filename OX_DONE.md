# OX_DONE — dashboard defaults to research face

Branch: `cursor/ox-dashboard-research-2ac0`

## Changes
- `web/dashboard/index.html`
  - Title: "Callisto — Research Appliance"; brand subtitle: "research appliance".
  - `panel-hyps`, `panel-orders`, `panel-portfolio` carry the `hidden` attribute.
  - First panel (`panel-state`) heading renamed to "System (loop health)" with a
    comment noting this view is loopback research status, not live betting.
- `web/dashboard/app.js`
  - `TRADING_MODE` from `?trading=1`; `applyTradingMode()` unhides the three
    trading panels only in that mode.
  - Money endpoint polls (`api/hypotheses/live`, `api/orders`, `api/portfolio`)
    are skipped entirely when trading panels are hidden; status, ingestion,
    alerts auto-refresh unchanged.
- `tests/test_dashboard_research_face.py` — 5 source-contract tests (no browser).

## Verification
`/tmp/callisto-pytest/bin/python -m pytest tests/test_dashboard_research_face.py -q`
→ 5 passed.

No edits to api.py / tools/autonomous.py / betting Python. No `/executor/enable`
reference anywhere.
