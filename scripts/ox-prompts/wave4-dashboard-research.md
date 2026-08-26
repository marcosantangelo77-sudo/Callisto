# OX TASK: dashboard default face is research, not LIVE betting

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-dashboard-research-2ac0`
Worktree: `/tmp/callisto-ox-dashboard-research`

## Exclusive files (HARD)

You MAY edit:
- `web/dashboard/index.html`
- `web/dashboard/app.js`
- `web/dashboard/styles.css` (only if needed for a hidden class)
- `tests/test_dashboard_research_face.py` (create — source contract, no browser)

You MUST NOT edit `api.py`, `tools/autonomous.py`, betting Python,
credentials, or `master`. Do NOT add an API that enables the executor.
Do NOT fetch `/executor/enable`. Do NOT show LIVE orders as the default above-the-fold story.

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.

## Why

The dashboard is titled "ops dashboard" and leads with LIVE hypotheses,
recent orders, and portfolio. Callisto's product is a personal research
appliance. Sports is the proving ground, not the face. Default HTML must
not look like a betting console.

## Required change (first slice only)

1. `index.html`:
   - `<title>` and brand subtitle: research appliance (not "ops dashboard").
   - Panels `panel-hyps`, `panel-orders`, `panel-portfolio` are **hidden by
     default** (`hidden` attribute or class). Visible only when
     `?trading=1` is in the query string (app.js unhides on load).
   - Add a panel **before** those: "Loop / seals" (or "Research") with
     static copy that this view is loopback research status, not live betting.
     If `api/status` already loads into `panel-state`, keep it first and
     rename heading from "Live state" to "System" or "Loop health".

2. `app.js`:
   - On load, if `URLSearchParams(location.search).get("trading") === "1"`,
     unhide the three trading panels. Otherwise do not fetch
     `api/hypotheses/live` / `api/orders` / `api/portfolio` (skip those
     polls — no need to hit money endpoints when hidden).

Keep auto-refresh for status/ingestion/alerts.

## Tests (`tests/test_dashboard_research_face.py`)

Read the HTML/JS as text:
- title/brand does not contain "ops dashboard" (case-insensitive).
- `panel-hyps` / `panel-orders` / `panel-portfolio` are hidden in HTML.
- `app.js` only unhides when `trading=1`.
- `app.js` does not reference `/executor/enable`.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_dashboard_research_face.py -q
```

Commit: `fix(ui): dashboard defaults to research face, not LIVE betting`

Write `OX_DONE.md`.
