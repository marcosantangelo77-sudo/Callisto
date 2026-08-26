# OX_DONE — delete LIVE/orders/portfolio HTML from dashboard

Branch: `cursor/ox-dashboard-delete-live-2ac0`

## What changed

- `web/dashboard/index.html`: deleted the `panel-hyps`, `panel-orders`, and
  `panel-portfolio` sections entirely. Kept loop/system, ingestion, alerts.
  Title/brand remain "research appliance".
- `web/dashboard/app.js`: removed TRADING_MODE, `applyTradingMode`,
  renderHyps/renderOrders/renderPortfolio, the `hyps`/`orders`/`portfolio`
  API entries, and their polls. Only status / ingestion / alerts are fetched.
  No `?trading=1` backdoor; no `/executor/enable`.
- `tests/test_dashboard_research_face.py`: rewritten as a deletion contract
  (panels absent, no "LIVE hypotheses" heading, no `api/hypotheses/live`
  poll, no money-endpoint fetches, research panels still present).
- `tests/test_fail_closed_registry.py`: only
  `test_dashboard_panel_hyps_is_hidden` replaced with
  `test_dashboard_trading_panels_are_absent` (asserts absence, not hidden).

## Verification

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_dashboard_research_face.py tests/test_fail_closed_registry.py -q
22 passed in 0.08s
```
