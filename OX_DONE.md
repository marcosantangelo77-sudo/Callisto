# OX_DONE

Split `tools/market_psychology.py` (1522 lines) into `tools/psych/`:

- `tools/psych/constants.py` — empirical constants (shade maps, scoring distributions, velocities, attention weights)
- `tools/psych/shading.py` — detect_number_shading, _shading_explanation
- `tools/psych/trap_lines.py` — detect_trap_line
- `tools/psych/futures.py` — futures_efficiency, optimal_hedge_time + private helpers
- `tools/psych/half_markets.py` — half_market_adjustment
- `tools/psych/attention.py` — attention_arbitrage
- `tools/psych/closing_line.py` — predict_closing_line, _clv_recommendation
- `tools/psych/_utils.py` — _prob_to_american
- `tools/market_psychology.py` — facade re-exporting the full original API; retains full_market_psychology

Tests: `tests/test_psych_split.py` — 13 tests covering facade re-exports,
submodule imports, and behavior of each function.

Verification: `/tmp/callisto-pytest/bin/python -m pytest tests/test_psych_split.py -q` → 13 passed.
All other importers (tools.clv_tracker, tools.autonomous, tools.api.*, api.py) import cleanly.
