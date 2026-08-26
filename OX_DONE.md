# OX_DONE.md — split tools/simulation.py into tools/sim/

Branch: `cursor/ox-sim-split-2ac0`
Commit: `refactor(sim): split simulation helpers into tools.sim`

## What was done

`tools/simulation.py` (1369 lines) was extracted into the `tools/sim/`
package. `tools/simulation.py` is now a thin compatibility facade that
re-exports every public name (and the private helpers used elsewhere,
e.g. `_classify_sport`, `_poisson_pmf`, `_make_edge_result`), so all
existing callers (`api.py`, `tools/edge_scanner.py`, tests) work unchanged.

### Note on tools/sim

A flat module `tools/sim.py` already existed (NBA/NFL/Poisson/prop sims used
by `tools/orch/sports_dispatch.py`, `scripts/e2e_final.py`,
`tests/test_integration_e2e.py`). The new package shadows it, so
`tools/sim/__init__.py` lazily re-exports its public API
(`nba_game_sim`, `nfl_game_sim`, `poisson_game`, `player_prop_sim`,
`compare_sim_to_book`, `sim_from_odds`) via a module-level `__getattr__`.
Verified: `import tools.orch.sports_dispatch` still succeeds.

## Line counts

| File | Lines |
|---|---|
| tools/simulation.py (facade, was 1369) | 58 |
| tools/sim/__init__.py | 91 |
| tools/sim/constants.py | 45 |
| tools/sim/models.py | 86 |
| tools/sim/game.py | 199 |
| tools/sim/markets.py | 200 |
| tools/sim/props.py | 120 |
| tools/sim/edge.py | 332 |
| tools/sim/pace_env.py | 179 |
| tools/sim/legacy.py | 196 |
| tests/test_sim_split.py | 203 |

Module responsibilities:
- **constants** — DEFAULT_ITERATIONS, sport classification, SPORT_DEFAULTS.
- **models** — TeamProfile / SimulationResult / PropSimResult / EdgeResult dataclasses.
- **game** — simulate_game + high/low-scoring models, _build_result, _poisson_pmf.
- **markets** — simulate_spread / simulate_total vs book lines.
- **props** — simulate_prop player prop engine.
- **edge** — EdgeResult construction (Wilson CI, Kelly), compare_to_book,
  compare_to_market, compare_poisson_to_market.
- **pace_env** — simulate_game_with_pace_env (pace model + venue/weather/refs).
- **legacy** — simulate_basketball, simulate_poisson.

## Tests

`tests/test_sim_split.py`: 14 tests covering re-export completeness,
implementation-location assertions (`__module__.startswith("tools.sim.")`),
and behavioral parity smoke tests for every moved function.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_sim_split.py -q   # 14 passed
... plus pre-existing tests/test_simulation.py                          # 31 passed total
```

No changes to api.py. No live betting armed. Full pytest suite not run (per task constraints).
