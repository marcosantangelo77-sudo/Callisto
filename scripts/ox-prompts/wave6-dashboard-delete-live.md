# OX TASK: delete LIVE/orders/portfolio HTML, do not hide it

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-dashboard-delete-live-2ac0`
Worktree: `/tmp/callisto-ox-dashboard-delete-live`

## Exclusive files (HARD)

You MAY edit:
- `web/dashboard/index.html`
- `web/dashboard/app.js`
- `web/dashboard/styles.css` (only if a now-unused rule must go)
- `tests/test_dashboard_research_face.py`
- `tests/test_fail_closed_registry.py` — ONLY `test_dashboard_panel_hyps_is_hidden` (rename/replace that one test; do not touch other tests in the file)

Do NOT edit `api.py`, `tools/autonomous.py`, betting Python, or `master`.
Do NOT add `/executor/enable`. Do NOT add a `?trading=1` backdoor that
reintroduces LIVE/orders/portfolio markup. Sports is the gym, not the face.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push this branch. Base origin/master.

## Why

Hiding `<section hidden>` still ships a sportsbook UI. 90/100 requires the
default dashboard HTML to contain **no** LIVE hypotheses / recent orders /
portfolio panels.

## Required

1. `index.html`: **delete** the `panel-hyps`, `panel-orders`, and
   `panel-portfolio` sections entirely (not `hidden`). Keep loop/system,
   ingestion, alerts. Title/brand stay "research appliance".
2. `app.js`: delete TRADING_MODE / `applyTradingMode` / renderers and polls
   for `API.hyps` / `API.orders` / `API.portfolio`. Stop fetching those
   money endpoints. Keep status / ingestion / alerts refresh.
3. Tests (source contract, no browser):
   - HTML has no `id="panel-hyps"` / `panel-orders` / `panel-portfolio`.
   - HTML/JS do not contain the heading "LIVE hypotheses" or an
     `api/hypotheses/live` poll.
   - JS has no `jsonFetch(API.orders)` / `jsonFetch(API.portfolio)`.
   - Still no `/executor/enable`.
   - Replace `test_dashboard_panel_hyps_is_hidden` so it asserts the
     three trading sections are **absent**, not merely hidden.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_dashboard_research_face.py tests/test_fail_closed_registry.py -q
```

Commit: `fix(ui): remove LIVE/orders/portfolio markup from dashboard`

Write `OX_DONE.md`.
