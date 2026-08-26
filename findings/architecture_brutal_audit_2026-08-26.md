# Architecture Brutal Audit — Callisto (/workspace)

**Date:** 2026-08-26  
**Checkout:** `cursor/brutal-codebase-audit-2ac0` (source re-verified; prior `ARCHITECTURE_MAP.md` / `AUDIT_MANDATE.md` treated as hints only)  
**Callisto API:** not running (`localhost:8420` unreachable) — audit is source-only.

Evidence tags: **VERIFIED** = measured against current tree; **INFERRED** = reasoned from VERIFIED facts.

---

## 1. Top 10 structural flaws

### 1. `tools/autonomous.py` — god process, not a module
**VERIFIED.** 8148 LOC; `ResearchLoop` alone has **53 methods** (`__init__`@1381 … `get_status`@8088). `_loop`@2445 (~409 lines) hard-sequences ~20 phases (`_phase_self_repair`, `_phase_backtest`, `_phase_generate_hypotheses`@3749 ~696 lines, `_phase_evaluate`@5117 ~543 lines, `_phase_claude_deep_work`@6781 ~415 lines, etc.). Fan-out ≈40 internal packages. Lazy `from api import historical_fetcher`@3303 and ~10× `escalate_with_ladder` imports.  
**Why it matters:** Every product concern (repair, data, hypgen, backtest, live money, wiki, regime, integrity) shares one class lifetime and one exception-swallowing loop. You cannot scale teams, test phases in isolation, or reason about failure domains.

### 2. Dual inference stacks — `ProviderRouter` does not own production AGP
**VERIFIED.** `Orchestrator` (`orchestrator.py`@40–47, `@981`) uses `get_architect`/`get_manager`/`get_sentinel` → `OllamaInference.achat` (@1217, @1243, @1418, …) plus `escalate_with_ladder` (@1350, @1526, @1663, @1788). `ProviderRouter.complete`@1548 is used by `callisto.py` → `tools/pipeline` and `agp/adversary.py`@478 — **not** by `api`/`Orchestrator`. `ResearchLoop` stores `loop_phase_task_classes`@1439–1443 and documents `router.complete` in `record_iteration_outcome`@7851–7867 but **never calls** `get_router()` / `ProviderRouter`. Parallel ladder: hardcoded `MODEL_LADDER` in `inference.py`@~150+ vs YAML pools in `config/providers.yaml`.  
**Why it matters:** Serving “decoupling” is theater for the hot path (API sessions + research loop). Two routing policies drift; provider.yaml changes do not move betting research.

### 3. Domain plugin architecture is half-wired theater
**VERIFIED.** Real extension type: `DomainPlugin` / `ToolRegistry` in `tools/domain_registry.py`@31–141. Production seed `_default_registry()`@663–675 registers **only** `build_sports_plugin(ODDS_TOOLS, …)` + `tools.domains.compute.register_if_available`. `tools/domains/finance/plugin.py` `register_if_available`@325 and `tools/domains/kalshi/plugin.py` `register_if_available`@221 are **never called from `orchestrator.py`** (only tests). Separately, `plugins/sports/` is a **schema** plugin (`tools/schema/__init__.py`@14–27), not the same as `tools/domains/sports.py` (49-line factory). Sports “plugin” still keeps a 200+ line `if name == …` chain in `_sports_tool_dispatch`@766–979 and **import-time** odds/scanner imports at `orchestrator.py`@51–91.  
**Why it matters:** Docs claim “adding a domain = register a plugin, never edit the orchestrator.” Finance/Kalshi exist on disk and in tests; live AGP sessions never get them. Coupling to sports remains at import time.

### 4. `api.py` is a 4704-LOC process kernel + 111-route facade
**VERIFIED.** `lifespan`@815 owns WriteCoordinator monkeypatch, schema, migrations, warmup, MemoryStore, TaskQueue, Orchestrator, LineMonitor, CLVTracker, AutonomousLoop, ResearchLoop, Telegram, hypothesis/backtest/embeddings/health, GameScheduler, event bus, order cron, etc. **111** `@app` routes (odds×21, research×8, orders×8, …). Module-level service locator globals (`orchestrator_instance`, `research_loop`, `autonomous`, `_executor`, …).  
**Why it matters:** HTTP layer = composition root = business logic dump. No bounded contexts; every feature adds another route and another global.

### 5. Event bus is a thin pub/sub bolted onto a god-orchestrator
**VERIFIED.** `tools/event_bus.py`@52 exists. Production **publish**: `line_monitor` (`EVENT_SNAPSHOT_TAKEN`, `EVENT_LINE_MOVED`), `data_collector` (`EVENT_GAME_COMPLETED`), `game_scheduler`. Production **subscribe**: only `ResearchLoop.start`@1471–1474 (`EVENT_GAME_COMPLETED`, `EVENT_GAME_LINEUP_WINDOW`). Constants `EVENT_EDGE_DETECTED`, `EVENT_BACKTEST_COMPLETE`, `EVENT_SHARP_MONEY`@39–41 are **never published** outside docstring/tests. Control plane remains `ResearchLoop._loop` sequential phases + `api.lifespan`.  
**Why it matters:** Not an event-driven architecture. Dead event types create false confidence; edge/backtest still synchronous inside the god loop.

### 6. Lazy import cycles via `api` service locator
**VERIFIED.** Zero import-time cycles (reconfirmed pattern). Function-level: `tools/telegram.py` `_cmd_order`@363 `import api as _api` → `_executor`; `tools/autonomous.py`@3303 `from api import historical_fetcher`; `tools/line_monitor.py`@1444 `from api import clv_tracker`. Telegram is fan-in notifier + back-edge into API state.  
**Why it matters:** Hidden coupling; refactoring `api` globals breaks tools that look “leaf.” Static import graphs lie.

### 7. Monkey-patched `aiosqlite` as architecture (`db_writer.install_aiosqlite_routing`)
**VERIFIED.** `tools/db_writer.py`@548–618 patches `aiosqlite.connect` / `Connection.execute` / `executemany` / `commit`. Installed first in `api.lifespan`@834–839. ~100 call sites across ~25 modules assumed.  
**Why it matters:** Global behavioral change to a third-party library. Scripts/tests that skip lifespan see different write semantics. Correct problem (single writer), wrong abstraction boundary (patch vs explicit writer API).

### 8. `BacktestEngine` monolith (4211 LOC / 30 methods)
**VERIFIED.** `run_backtest`@170 owns spring-training gates, multibook enrich, filter DSL (`_parse_hypothesis_filters`@1225, `_matches_hypothesis_conditions`@1425), schedule context (`_build_schedule_context`@1586), props (`_process_game_props`@2564), resolution (`resolve_with_scores`@2917, `resolve_from_game_results`@3273), stats recalc, paper signals (`generate_paper_trade_signal`@3799). Couples tightly to `HypothesisManager`.  
**Why it matters:** Un-testable kernel of money truth; any filter/resolution change risks silent lookahead or promotion poison.

### 9. Money-path duplication — `tools.kelly` vs `tools.sizing`
**VERIFIED.** `tools/sizing.py`@11–19 deprecates itself toward `tools.kelly` yet still exports `kelly_binary`, `kelly_with_push`, `uncertainty_adjusted_kelly`. `BetExecutor.compute_stake`@276–277 imports **both**. Backtest uses `tools.sizing.kelly_binary`. Orchestrator tool `bet_size` → `sizing.bet_size_american`.  
**Why it matters:** Two Kelly definitions on the live path → inconsistent stakes and audit nightmares.

### 10. `HypothesisManager.check_promotion_readiness` / `auto_promote` as policy megafunctions
**VERIFIED.** `HypothesisManager`@474, 2962 LOC, 26 methods. `check_promotion_readiness`@988 is a long inline gate checklist (min_days, significance, CLV, overlap `_compute_portfolio_overlap`@1773, …); `auto_promote`@1825 continues. Status machine + SQL + bankroll sim + wiki side effects in one class.  
**Why it matters:** Promotion is the risk control plane. A single class owning draft→live is a single point of silent gate bypass (history already shows unread gate keys).

---

## 2. God-module table

| File | LOC | Fan-out (internal pkgs, approx) | Split into |
|------|-----|----------------------------------|------------|
| `tools/autonomous.py` | 8148 | ~40 | `research/loop.py`, `research/phases/{collect,hypgen,backtest,evaluate,live,repair,wiki}.py`, `autonomous/edge_loop.py`, `research/startup_migrations.py` |
| `api.py` | 4704 | ~61 | `app/lifespan.py`, `app/workers.py`, `routers/{tasks,odds,bets,hypothesis,research,health,orders,admin}.py`, `app/deps.py` (no module globals) |
| `tools/backtest.py` | 4211 | ~13 | `backtest/engine.py`, `backtest/filters.py`, `backtest/context.py`, `backtest/props.py`, `backtest/resolve.py`, `backtest/paper.py` |
| `tools/hypothesis.py` | 2962 | ~11 | `hypothesis/store.py`, `hypothesis/gates.py`, `hypothesis/promote.py`, `hypothesis/reports.py` |
| `orchestrator.py` | 1957 | ~27 | `agp_runtime/session.py`, `agp_runtime/steps/*.py`, move `_sports_tool_dispatch` → `tools/domains/sports/dispatch.py`; drop import-time odds |
| `inference.py` | 1776 | ~9 | `inference/ollama_agent.py`, `inference/ladder.py` (delete or adapter), `inference/router.py`, `inference/config.py` |
| `tools/bet_executor.py` | 1253 | ~8 | `execution/sizing_bridge.py`, `execution/browser.py`, `execution/risk.py`, `execution/ledger.py` |
| `tools/self_repair.py` | 1035 | ~9 | `repair/detectors.py`, `repair/actions.py`, `repair/findings.py` |
| `tools/clv_tracker.py` | 943 | ~7 | Keep; extract telegram side effects |
| `plugins/sports/schema.py` | 1291 | — | Already schema-only; do not confuse with domain tools |

---

## 3. Concrete refactoring — 3 worst modules

### A. `tools/autonomous.py` → phase worker registry

```
tools/research/
  loop.py              # ResearchLoop: schedule + pause + status ONLY
  bus_handlers.py      # _on_game_completed / lineup
  startup.py           # _migrate_edge_thresholds, _requeue_*, _reject_*
  phases/
    base.py            # Protocol: async def run(ctx: ResearchContext) -> PhaseResult
    self_repair.py
    collect.py
    hypgen.py          # from _phase_generate_hypotheses
    backtest.py
    evaluate.py
    live_execute.py
    claude_deep_work.py
    wiki.py
  context.py           # deps: hypothesis_manager, backtest_engine, line_monitor — injected
tools/autonomous/edge_loop.py  # AutonomousLoop only
```

`ResearchContext` holds injected services (never `from api import …`). Each phase is a module with one entrypoint; `_loop` becomes `for phase in SCHEDULE: await phase.run(ctx)`.

### B. `api.py` → FastAPI composition root only

```
app/
  main.py              # create_app(); include_routers
  lifespan.py          # current lifespan body
  state.py             # typed AppState dataclass on app.state — kill module globals
  workers/
    task_worker.py     # task_worker, _run_session_with_adaptive_timeout
    order_cron.py
    wal_checkpoint.py
  routers/
    tasks.py           # /task, /session, /world, /context/sync
    odds.py            # /odds/*, /edges/live
    money.py           # /bets/*, /executor/*, /orders/*
    research.py        # /hypothesis/*, /backtest/*, /research/*
    health.py
    admin.py
```

Endpoints become thin: `Depends(get_state)` → call domain service. Telegram/order code takes `BetExecutor` via constructor, not `import api`.

### C. `inference.py` — one serving façade

```
inference/
  router.py            # ProviderRouter only (keep)
  agents.py            # OllamaInference OR delete after migration
  legacy_ladder.py     # escalate_with_ladder — temporary adapter
```

**Mandatory:** implement `escalate_with_ladder` as thin wrapper:

```python
async def escalate_with_ladder(...):
    router = get_router()
    return await router.complete(task_class=TASK_TYPE_ALIASES[task_type], messages=...)
```

Point `Orchestrator._step_*` and all `ResearchLoop` call sites at `router.complete`. Delete duplicate `MODEL_LADDER` once parity tests pass. `warmup_models` must warm router endpoints, not only Ollama agent configs.

---

## 4. What is actually well-designed (VERIFIED only)

1. **`tools/domain_registry.py`** — small, dependency-free `DomainPlugin`/`ToolRegistry` with clear `serves` / `dispatch` contracts (@31–131). The *abstraction* is real; production wiring is incomplete.
2. **`ProviderRouter`** (`inference.py`@1089–1669) + `config/providers.yaml` — real pool/failover/budget/empirical reorder. Used correctly by `callisto.py` + `tools/pipeline` + `agp/adversary.py`.
3. **`agp/` package** (~3.7k LOC across modules) — session seal/HMAC notes, provenance (`agp/provenance.py`), thresholds, adversary seam — cohesive protocol core vs the tools megablob.
4. **`tools/db_writer.WriteCoordinator`** — correct diagnosis of SQLite single-writer; queue + owned connection (@78+) is the right *idea* (monkeypatch is the smell).
5. **`callisto.py` front door** — clean CLI wiring of Router → ResearchPipeline without dragging in `api.py` globals (@70–80).
6. **Schema split** — `tools/schema/core.py` vs `plugins/sports/schema.py` registration is a real plugin boundary for DDL (different from domain tools, but real).

---

## Verdict

Callisto is a **betting research monolith** with a **second, cleaner AGP CLI pipeline** bolted beside it. Plugin/domain/event-bus/`ProviderRouter` narratives are partially true in isolated packages and **false on the production path** that `api.lifespan` starts (`ResearchLoop` + `Orchestrator` + Ollama ladder). Scaling failure mode is not GPU count — it is inability to change one phase, one gate, or one provider without touching 2–8k LOC hubs.
