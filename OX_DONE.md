# OX DONE — orchestrator.py pipeline split

Branch: `cursor/ox-orch-split-2ac0`

## Line counts

| File | Before | After |
|---|---|---|
| orchestrator.py | 1957 | 1051 |
| tools/orch/tool_schemas.py | — | 565 |
| tools/orch/sports_dispatch.py | — | 246 |
| tools/orch/pipeline_support.py | — | 191 |
| tests/test_orch_split.py | — | 84 |

Net: ~900 lines of real code (tool schemas, sports tool dispatch, registry
seed, freshness routing, parallel-search staging, domain query refinement,
pure helpers) moved out of orchestrator.py into `tools/orch/`.

## What moved

- `tools/orch/tool_schemas.py` — all Ollama native-tool-calling JSON schemas
  (`WEB_SEARCH_TOOL`, `ODDS_TOOLS`, …) + `HERMES_TOOL_PROMPT`. Pure data.
- `tools/orch/sports_dispatch.py` — `_sports_tool_dispatch` verbatim (all ~25
  sports tool implementations).
- `tools/orch/pipeline_support.py` — `_default_registry`, `_registry_seeded`,
  `_execute_sports_tool`, `_detect_freshness`, `_json_compact`, `_safe_parse`,
  `_parse_domain`, `_clamp_confidence`, `_best_source_class`,
  `_dedup_search_results`, `MAX_TOOL_CALL_ROUNDS`, plus extracted
  `run_searches_parallel` and `domain_search_query`.

orchestrator.py keeps the full `Orchestrator` class and re-exports every
previously-public name as a facade.

## Contracts preserved

- `from orchestrator import Orchestrator` works (api.py untouched).
- All old names (`ODDS_TOOLS`, `_clamp_confidence`, `_default_registry`,
  `_detect_freshness`, `_execute_sports_tool`, thresholds, etc.) still import
  from `orchestrator`.
- Seal path unchanged: `run_session` still calls keyed `session.seal()` with
  `AGPSealRefused` handling; no unkeyed verify path introduced.

## Tests

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_orch_split.py tests/test_agp_seal.py tests/test_preregistration_seal.py -q
31 passed
```

Plus regression: tests/test_confidence.py, test_build_b3_registry_grounding.py,
test_claude_code.py, test_integration_e2e.py, test_tier3_epi_trust.py —
50 passed.
