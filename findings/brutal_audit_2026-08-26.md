# Brutal 5-dimension audit — Callisto

**Date:** 2026-08-26
**Checkout:** `master` @ `245d9f6` (audit branch `cursor/brutal-codebase-audit-2ac0`)
**API:** not running (`localhost:8420` unreachable). Source-only.
**Evidence tags:** **VERIFIED** = re-read or measured against this tree. **INFERRED** = reasoned from VERIFIED facts. Prior docs (`ROADMAP.md`, `AUDIT_MANDATE.md`, `ARCHITECTURE_MAP.md`) were treated as hints, not authority.

Dimension 1 detail: `findings/architecture_brutal_audit_2026-08-26.md`.

---

## Process (orchestrator)

Operator instruction: Ox Alpha workers inspect; this process orchestrates.

What actually ran:

1. Hermes Agent CLI was installed (`~/.local/bin/hermes`). `hermes -z --provider nous -m stealth/ox-alpha` failed: **not logged into Nous Portal**. No `NOUS_API_KEY` / Hermes OAuth on this VM. Credentials were requested via environment setup; none arrived during this pass.
2. Four parallel inspectors (architecture, health, performance, security) ran against `/workspace`. Every CRITICAL/HIGH claim below was **re-derived by the orchestrator** against current source. Inspector prose that did not survive that check is omitted.
3. `ORCHESTRATION_HANDOFF.md` candidate reviews (`dbcc751`, `1ec9778`) were **not** started. This file is the checklist gate.

Do not treat this document as an OX-worker self-report. Citations are file:line on this tree.

---

## 1. Architecture & design patterns

### What this system actually is

Two stacks live in one repo and only one of them is the process `start.bat` / `api.py` lifespan boots:

| Path | Entrypoint | Inference | Status |
|---|---|---|---|
| **Workstation kernel** | `api.py` lifespan → `ResearchLoop` + `Orchestrator` | `escalate_with_ladder` / `MODEL_LADDER` / Ollama agents | what runs unattended |
| **CLI / pipeline** | `callisto.py` → `tools/pipeline` | `ProviderRouter.complete` + `config/providers.yaml` | real router; not the kernel |

**VERIFIED.** `Orchestrator` constructs `get_architect()` (`orchestrator.py:986`) and calls `escalate_with_ladder` at `:1350`, `:1526`, `:1663`, `:1788`. `ResearchLoop` imports `escalate_with_ladder` at nine sites (`autonomous.py:2107`, `:3107`, `:3808`, `:4142`, `:4271`, `:6065`, `:6796`, `:7532`, `:7991`) and **never** calls `get_router()` / `ProviderRouter.complete`. `loop_phase_task_classes` (`autonomous.py:1439`) and the `record_iteration_outcome` docstring (`:7860–7867`) describe a router that this class does not use.

`providers.yaml` is not a lie — it is a **second control plane**. Editing it does not move the betting research loop.

### God modules (VERIFIED LOC)

| File | LOC | Role |
|---|---|---|
| `tools/autonomous.py` | 8148 | Unattended loop: repair, collect, hypgen, backtest, evaluate, live money, wiki |
| `api.py` | 4704 | Composition root + 111 routes + service-locator globals |
| `tools/backtest.py` | 4211 | Filter DSL, schedule context, props, resolution, paper signals |
| `tools/hypothesis.py` | 2962 | Store + promotion policy + retroactive evidence rewrite |
| `orchestrator.py` | 1957 | AGP steps + sports tool dispatch |
| `inference.py` | 1776 | Ollama agents + `MODEL_LADDER` + `ProviderRouter` (unrelated consumers) |

### Structural flaws that will not scale

1. **`ResearchLoop` is the product.** `_loop` (`autonomous.py:2445`) sequences ~20 `_phase_*` methods on one object. Failures are swallowed at phase granularity. You cannot ship a phase, test a phase, or bound a phase's blast radius without loading the class.
2. **Event bus is costume.** `EventBus` exists. Production subscribe: `ResearchLoop.start` (`:1471–1474`) on game-completed / lineup. `EVENT_EDGE_DETECTED` / `EVENT_BACKTEST_COMPLETE` / `EVENT_SHARP_MONEY` are not published on the production path. Control remains the sequential loop.
3. **Plugin architecture is half-wired.** `tools/domain_registry.py` is a real registry. `_default_registry()` registers sports + compute. `tools/domains/finance/plugin.py:325` and `tools/domains/kalshi/plugin.py:221` `register_if_available` are test-only. `orchestrator.py:51–91` still import-time couples odds/scanners; `_sports_tool_dispatch` (`:766–979`) is a 200-line `if name ==` chain. `plugins/sports/` is DDL, not the domain plugin.
4. **`install_aiosqlite_routing` (`tools/db_writer.py:548`)** monkeypatches `aiosqlite.Connection`. Correct diagnosis (one writer), wrong boundary (global patch of a third-party class; scripts that skip `lifespan` see different semantics).
5. **Lazy cycles through `api` as a service locator.** `telegram.py:363` `import api`; `autonomous.py:3303` `from api import historical_fetcher`; `line_monitor.py:1444` `from api import clv_tracker`. Zero import-time cycles; coupling is hidden from static graphs.
6. **Two Kelly stacks on the money path.** `tools/kelly.py:140` `kelly_full(edge, american_odds)` does `p = implied + edge`. `tools/sizing.py:34` `kelly_binary(fair_prob, decimal_odds)` is a different contract. `BetExecutor.compute_stake` (`bet_executor.py:276–277`) imports both. Backtest uses `sizing.kelly_binary`.

### Refactoring pattern — three worst modules

**A. `tools/autonomous.py` → phase registry, not a god class**

```
tools/research/
  loop.py          # schedule, pause, status only
  context.py       # injected deps; forbid `import api`
  phases/*.py      # one module per _phase_*; Protocol with run(ctx) -> PhaseResult
  startup.py       # migrations currently buried in ResearchLoop.__init__
```

`_loop` becomes `for phase in schedule: await phase.run(ctx)`. No phase may `except Exception: pass`.

**B. `api.py` → composition root only**

```
app/factory.py     # create_app(), lifespan
app/state.py       # typed AppState; kill module globals
routers/*.py       # one router per bounded context
```

Pass `BetExecutor` into Telegram by constructor. Telegram must not `import api`.

**C. `inference.py` → one router**

Make `escalate_with_ladder` a thin adapter:

```python
async def escalate_with_ladder(prompt, system_context="", task_type="reasoning", **kw):
    alias = TASK_CLASS_ALIASES.get(task_type, "research_synthesis")
    return await get_router().complete(
        alias, [{"role": "user", "content": prompt}],
        system_context=system_context,
    )
```

Migrate `Orchestrator` / `ResearchLoop` call sites. Delete `MODEL_LADDER` after parity tests. Until that lands, `providers.yaml` is documentation for a process that is not running.

### What is actually well-designed (VERIFIED)

- `WriteCoordinator` queue + stop-settle (`tools/db_writer.py`) — right idea, even if the patch is the smell.
- `require_admin` fails closed when `CALLISTO_ADMIN_TOKEN` is unset (`api.py:101–107`); timing-safe compare (`:111`); no `X-Forwarded-For` trust (`:77–85`).
- Learning-layer seal (`tools/memory_epistemics.py:162–174`) refuses unkeyed digests. Session-layer `AGPSession.verify_seal` does not (see §4).
- `callisto.py` CLI front door actually uses `ProviderRouter`.
- Bounded caches exist in line_monitor / autonomous / deferred work (see §3). They do not save the event loop.

---

## 2. Code health & maintainability

### Measured

| Measure | Result | Tag |
|---|---|---|
| `except Exception` in `tools/` | 784 | VERIFIED (inspector count; order-of-magnitude confirmed by spot-check: `autonomous.py` alone is a swamp) |
| `tools/autonomous.py` | 8148 LOC, 24 `_phase_*` | VERIFIED |
| Modules >500 LOC with no `tests/test_<stem>.py` | majority of money/loop modules | VERIFIED for `autonomous`, `backtest`, `line_monitor`, `kelly`, `self_repair` |
| Root junk | empty `0)`, `20`, `40`, `50`, `10.3f}'` | VERIFIED (shell-glob accidents) |
| `_dbg_s2.py`, `_dbg_s4.py`, `_dbg_x.py` | debug debris in tree | VERIFIED |

### Defect 1 — unattended gate saw (CRITICAL, SILENT)

Self-repair's *named* lowering strategies are refused (`self_repair.py:983–1010`). The promotion engine was not.

`HypothesisManager.auto_promote` (`hypothesis.py:1893–1954`):

- On 0 signals after ≥2 eval cycles, if `_diagnose_edge_threshold` says `threshold_too_high`, it **writes `hypotheses.edge_threshold`** (`:1906–1908`).
- Then **rewrites historical `backtest_events.signal_generated`** from the new threshold (`:1917–1923`).
- Then syncs `backtest_runs.signals_generated` (`:1937–1945`).
- Then, if enough signals appear, checks promotion immediately (`:1955–1962`).

This is not behind `CALLISTO_ALLOW_THRESHOLD_MIGRATION`. It is not a diagnostic. It mutates the operative gate and the evidence that gate reads.

Amplifier: `_phase_refresh_signals` (`autonomous.py:3193–3214`) runs every cycle:

```sql
UPDATE backtest_events SET signal_generated = 1
WHERE id IN (
  SELECT be.id FROM backtest_events be
  JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id
  WHERE be.edge >= h.edge_threshold AND be.edge > 0
    AND be.signal_generated = 0)
```

Comment claims "Claude deep work can lower edge_threshold". Those paths refuse lowering. This phase still upgrades history for **any** threshold drop, including `auto_promote`.

**Blast radius:** SILENT. Promotion metrics move. Tests in `test_tier1_loop_self_repair_gate_policy.py` pin the self_repair refusers and do not cover `auto_promote`.

**Falsifier:** a test that drives `auto_promote` with events below threshold and asserts `edge_threshold` and `signal_generated` are unchanged.

### Defect 2 — unread writes still stamp confidence 0.8

`_fix_finding_low_sample` (`self_repair.py:946–979`) writes `model_config.minimum_events = 30`. Repo-wide `minimum_events` readers: this function and comments/tests about the *other* key `minimum_events_for_promotion`. Backtest/promote do not read it.

`_fix_finding_prioritize_sports` (`:928–944`) records a Hermes learning and returns `fixed=True`. `_record_to_hermes` (`:1024–1025`) stamps `confidence=0.8 if fixed`.

`tests/test_claude_findings.py` pins `fixed is True` for sport-priority. The suite locks in the lie.

### Defect 3 — exception swallowing as control flow

`autonomous.py` wraps phases so the loop always continues. Example adjacent to money/promotion (`:6133` region, `_phase_evaluate`): `except Exception: pass` around significance so Claude sees rows without p-values. `event_bus.py` subscriber failures are `except Exception: pass`. Self-repair heartbeat Telegram/Hermes failures vanish the same way (`self_repair.py:167–177`).

An unattended loop that cannot fail closed will not tell you it is wrong.

### Defect 4 — test suite honesty

**Pins real arithmetic:** `tests/test_tier0_money_kelly.py` (explicitly characterization, including `p = implied + edge`), `tests/test_devig.py`, self-repair refusers, some promotion-readiness cases.

**Pins policy theater:** sport-priority `fixed=True`; `test_legacy_unkeyed_seal_still_verifies_under_keyed_regime` (`tests/test_tier3_epi_seal.py:115–124`) **requires** `verify_seal` to accept a public SHA-256 after `CALLISTO_SEAL_KEY` is set. That is the opposite of integrity.

**Invisible to pytest:** `tests/test_integration_e2e.py`, `tests/test_full_system_audit.py` — no `def test_*`. Named like tests; CI will not run them.

### Concrete patch — stop `auto_promote` from moving the gate

```diff
# tools/hypothesis.py  (~1896)
-                    if edge_diag.get("threshold_too_high"):
-                        new_threshold = edge_diag["recommended_threshold"]
-                        ...
-                        UPDATE hypotheses SET edge_threshold = ?
-                        UPDATE backtest_events SET signal_generated = ...
+                    if edge_diag.get("threshold_too_high"):
+                        logger.warning(
+                            "threshold_too_high hyp=%s current=%s max_edge=%s "
+                            "recommended=%s — diagnose only, no write",
+                            hypothesis_id,
+                            edge_diag.get("current_threshold"),
+                            edge_diag.get("max_edge"),
+                            edge_diag.get("recommended_threshold"),
+                        )
```

Delete or flag-gate `_phase_refresh_signals`. A maintenance routine must not rewrite `signal_generated`.

---

## 3. Performance & bottlenecks

### Critical path risks

| # | Risk | Complexity | Cite | Blast |
|---|---|---|---|---|
| 1 | Hermes CLI: fresh process per completion, `max_concurrency: 1`, ~14s startup | O(calls)×~14s | `config/providers.yaml:100–106`, `tools/pipeline/hermes_cli.py:15–40` | **Freeze** of CLI/pipeline/adversary when proxy is down. Kernel still uses `MODEL_LADDER`, so this hits `callisto.py` / OX failover, not `ResearchLoop` — until someone "fixes" dual inference by pointing the loop at CLI. |
| 2 | `/simulate/portfolio` runs sync Monte Carlo **on the FastAPI loop** + sync `sqlite3.connect` | O(n_sims × horizon × portfolio); caps 5000×365 | `api.py:2507–2539`, `tools/bankroll_sim.py:509–523` | **Freeze**. Watchdog `/health` stalls. |
| 3 | `cluster_by_similarity`: full N×N cosine + single-linkage that rescans cluster members | O(N²) memory, worst-case ~O(N³) | `tools/embeddings.py:635–661` | **OOM** then **slow** |
| 4 | `VectorStore.search` loads every embedding in the collection | O(N·d) per query | `tools/embeddings.py:339–393` | **Slow** / **OOM** as wiki/memory grows |
| 5 | Paper-trade CLV backfill: per-trade `closing_lines` then snapshot JSON parse | N+1 + fat blobs | `tools/data_collector.py:1232–1367` | **Slow** autonomous resolve |
| 6 | Prop resolve: per-trade SELECT then `DISTINCT` player scan + `SequenceMatcher` | O(trades × players) | `tools/data_collector.py:896–971` | **Slow** |
| 7 | Odds snapshot: dual `json.dumps` of full multi-book payloads every interval | O(payload) CPU+I/O | `tools/line_monitor.py:1270–1314` | Writer-queue pressure |
| 8 | `/system/full-status`: multiple `COUNT(DISTINCT)` over `backtest_events` | O(table) | `api.py:3931–4022` | Session-start poll contends with writers |
| 9 | `deferred_work_queue`: **no indexes** | table scan on every enqueue cap check | `tools/work_queue.py:99–129` | Pending cap 50 hides the scan today; it will not stay 50 |
| 10 | `hypotheses`: unique on `name` only; **no `status` index** | full-status + list-by-status | `plugins/sports/schema.py` hypotheses DDL | Grows with every rejected hyp |

### Event-loop blocking (VERIFIED)

- `api.py` has **no** `asyncio.to_thread` / `run_in_executor`.
- `simulate_portfolio` (`:2533`) is CPU-bound numpy nested loops, called directly from the handler.
- `write_health_file()` on every `/health` (`:3712–3713`).
- `/health/detailed` and `/regime/sizer-multipliers` call sync `detect_regime` → `sqlite3.connect`.
- Mixed `sqlite3.connect` in `market_regime`, `bankroll_sim`, etc. bypasses `WriteCoordinator` → WAL writer lock contention with the API writer.

### Already correctly bounded (VERIFIED)

WriteCoordinator `maxsize=10000` (`db_writer.py:91`); deferred work pending cap 50 (`work_queue.py:119–125`); Hermes proc semaphore default 3 (`hermes_cli.py:38–55`); line_monitor alert deque 100 / KL cache 2000; autonomous LRU caps; task_queue `(status, priority DESC, created_at)` index.

Queues were hunted. The event loop and the algorithms were not.

### Concrete fixes

```python
# api.py simulate_portfolio_endpoint — get off the loop
result = await asyncio.to_thread(
    simulate_portfolio,
    hypothesis_ids=ids, n_sims=n_sims, horizon_days=horizon_days,
    starting_bankroll=starting_bankroll, kelly_fraction=kelly_fraction,
)
```

```sql
CREATE INDEX idx_dwq_pending ON deferred_work_queue(status, priority, created_at);
CREATE INDEX idx_hypotheses_status ON hypotheses(status);
CREATE INDEX idx_paper_sport_date_result ON paper_trades(sport, game_date, actual_result);
CREATE INDEX idx_player_stats_sport_date_type ON player_stats(sport, game_date, stat_type);
```

`cluster_by_similarity`: stop materializing N×N; Union-Find over kNN. `VectorStore.search`: do not SELECT `embedding_json` when blob exists; add an ANN index or a memory-mapped float32 matrix.

---

## 4. Security & data integrity

No live secrets in-repo (**VERIFIED**: `.env` gitignored, `.env.example` placeholders only, no `sk-` hardcodes). `shell=True` not used on surveyed production paths. `/admin/sql` is AST-allowlisted + `PRAGMA query_only=ON` + `require_admin` (**VERIFIED** `api.py:4389–4435`).

That is the good news. It stops there.

### C1 — keyed seals still accept a public SHA-256 (CRITICAL)

```482:487:agp/__init__.py
        candidates = [_seal_digest(payload), hashlib.sha256(payload.encode("utf-8")).hexdigest()]
        for key in _seal_keys():
            candidates.append(
                hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
            )
```

When `CALLISTO_SEAL_KEY` is set, `_seal_digest` is HMAC. Candidate list **still includes** `sha256(payload)`. A DB-write attacker rewrites `full_session` and sets `seal_hash` to the public digest. `AGPSession.verify_seal` returns True. `GET /session/{id}` / `memory.get_session(verify=True)` accept it.

Learning admission already closed this (`memory_epistemics.py:169–174`: only `"keyed"` / `"rotation"`). Session verify did not.

The test suite **pins the hole**: `test_legacy_unkeyed_seal_still_verifies_under_keyed_regime`.

**Exploit condition:** DB write (local disk, stolen admin, another process). No network required. Key may be set.

**Patch:**

```python
payload = _canonical_payload(data)
stored_hash = data.get("seal_hash")
keys = _seal_keys()
if keys:
    candidates = [_seal_digest(payload)]
    for key in keys[1:]:
        candidates.append(hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest())
    # do NOT append unkeyed sha256
else:
    candidates = [hashlib.sha256(payload.encode("utf-8")).hexdigest()]
return any(hmac.compare_digest(c, stored_hash) for c in candidates)
```

Migrate legacy rows offline. Invert the test: keyed regime must **reject** unkeyed digests.

### C2 — process binds all interfaces; GETs are ungated (CRITICAL)

`api.py:64` defaults `CALLISTO_BIND_HOST=127.0.0.1`. Launchers ignore it:

- `start.bat:85–87` — `uvicorn api:app --host 0.0.0.0 --port 8420`
- `scripts/overnight_setup.py:59` — same

Write middleware (`api.py:1242–1274`) only inspects POST/PATCH/PUT/DELETE. Ungated GETs on a reachable bind:

| Route | Leak |
|---|---|
| `/system/full-status` (`:3931`) | loops, integrity, research, queue |
| `/hypothesis` / `/hypothesis/{id}` (`:3155`, `:3162`) | full hyp objects |
| `/bets`, `/bets/bankroll` (`:2012`, `:2018`) | money ledger |
| `/executor/status` (`:4476`) | whether cash placement is armed |

**Exploit condition:** `start.bat` / overnight setup on a LAN- or WAN-reachable host. Default Windows launch path.

**Patch:** `start.bat` and `overnight_setup.py` must pass `%CALLISTO_BIND_HOST%` defaulting to `127.0.0.1`. Put `Depends(require_admin_or_loopback)` on the GET list above.

### C3 — Telegram `/resume_all` arms the cash executor (CRITICAL)

```152:161:tools/telegram_bot.py
async def _cmd_resume_all(...):
    manager.enable()
    if bet_executor is not None:
        try:
            bet_executor.enable()
```

Auth is chat-ID equality (`telegram.py:293–294`). No confirm phrase. No admin Bearer. `/pause_all` disables both; `/resume_all` re-enables both.

`BetExecutor.__init__` defaults `_enabled = False` (`bet_executor.py:165`). `POST /executor/enable` is `require_admin` (`api.py:4483`). Telegram bypasses that.

**Exploit condition:** bot token + `TELEGRAM_CHAT_ID` (stolen session, mis-set chat). Combined with a future one-line "accept live" in `generate_paper_trade_signal`, this is the arming switch.

**Patch:** `/resume_all` enables `order_manager` only. Require `CONFIRM_LIVE` **and** HTTP admin for `bet_executor.enable()`. `OrderManager._enabled` currently defaults **True** (`order_manager.py:201`) — default it False.

### H1 — `/admin/restart` is soft-gated

`api.py:4129` uses `require_admin_or_loopback`. Loopback + `?confirm=YES` → `os._exit(0)` (`:4157–4160`). Local malware / local CSRF / compromised MCP. Hard-gate with `require_admin`; keep the signal-file path for tokenless local ops.

### H2 — `force=true` skips promotion gates through live

`PATCH /hypothesis` (`api.py:3249–3274`): `force=true` skips `check_promotion_readiness` and can set `status=live`. Gated by `require_admin` (fail-closed if token unset). Still a single-token path around the statistical gate. Refuse `force` for `→ live`.

### H3 — Playwright selector interpolation

`bet_executor.py:713–717` builds selectors from `selection_text` / team names. If executor is armed and a signal carries attacker-influenced text, the click target is attacker-influenced. Use role locators / exact text, never interpolate into CSS `:has-text('...')`.

### H4 — `joblib.load` of models with no path allowlist

`tools/ml_classifier.py:612–613`. Pickle. Constrain to the models dir; prefer skops/onnx.

### H5 — SSRF on source fetch

`tools/sources/base.py` `RestSource.get` and `tools/sources/wayback.py:75–78` fetch caller-influenced URLs with no private-IP / scheme deny-list. Research-path SSRF if the host has egress to metadata/link-local.

### Money-path safety (do not "fix")

Two facts, both VERIFIED:

1. **Unattended live loop cannot collect signals today.** `_phase_live_execute` (`autonomous.py:5915–5918`) calls `generate_paper_trade_signal`, which returns `[]` unless `status == "paper_trading"` (`backtest.py:3808–3810`). Live hyps always yield an empty batch. ROADMAP §0 still holds. **Do not accept `live` in that function.**
2. **Placement code is not dead.** `BetExecutor.execute_bet` / `place_bet_on_slip` exist. Defaults off. Armed by HTTP admin **or** Telegram `/resume_all`. Fallback path when order_manager is disabled (`autonomous.py:6031`) clicks Place Bet. Paper path (`:6766`) is separate and does not call `execute_bet`.

Fail-safe is accidental. The one-line status change is the loaded gun. Characterization tests for sizing/CLV/caps must exist *before* anyone discusses arming.

`CALLISTO_LOCAL_ONLY` kills Claude subprocesses (`claude_code.py`). It does **not** disable the executor.

---

## 5. Brutal verdict & pragmatic outlook

### Production Readiness Score: **29 / 100**

This is not a 10. There is a real single-writer SQLite coordinator, fail-closed admin tokens, timing-safe compares, `/admin/sql` that is not a write oracle, executor-off-by-default, bounded queues, and a test tree that actually characterizes Kelly arithmetic.

This is not a 50. Production would require: one inference control plane; one Kelly; seals that mean "Callisto sealed this"; bind addresses the launcher cannot override into `0.0.0.0`; gates that cannot rewrite their own evidence; an event loop that cannot be stalled by a 5000×365 Monte Carlo; an unattended loop that can fail closed. All of those are false on the path `start.bat` boots.

29 is "dangerous research workstation with some scar tissue from prior audits." It is not a service. It is not a trading system. Shipping it to a network or to live money without the roadmap below is malpractice.

### The 3 critical flaws

**1. Freeze under load — event loop + god loop**

`/simulate/portfolio` and sync sqlite/regime/health-file I/O run on the FastAPI loop (`api.py:2507–2539`, `:3712`, `:3794`). `ResearchLoop` is 8148 lines of `except Exception` continue. One Monte Carlo or one wedged phase and `/health` stops answering; the watchdog's picture of life is a lie. Hermes CLI failover (~14s/fork, concurrency 1) will freeze the *other* stack the first time someone unifies routing by pointing the kernel at OX CLI.

**2. Breach — bind + ungated reads + forgeable seals**

`start.bat` publishes the API on `0.0.0.0`. Full-status, hypotheses, bets, executor status have no GET auth. Independently, `verify_seal` accepts a public SHA-256 while advertising HMAC. Integrity of sealed sessions is a UI checkbox. Tests enforce the weakness.

**3. Silent gate / accidental money switch**

`auto_promote` lowers `edge_threshold` and rewrites `signal_generated`. `_phase_refresh_signals` amplifies it every cycle. Self-repair tests congratulate themselves for refusing a different path. Telegram `/resume_all` enables Playwright betting. The live signal collector is dead only because of a status string. Those two facts are one bad diff apart from a live book.

### Refactoring roadmap (Risk / Effort)

| Rank | Fix | Risk if skipped | Effort | Notes |
|---|---|---|---|---|
| 1 | Bind `127.0.0.1` in `start.bat` + `overnight_setup.py`; gate sensitive GETs | Network data leak **now** on the Windows launch path | **Trivial** | Do this before any other work. |
| 2 | `verify_seal`: drop unkeyed candidate when a key is set; invert the pinning test | Forged AGP sessions while claiming HMAC | **Small** | Learning layer is the template. Offline-migrate legacy rows. |
| 3 | Strip `bet_executor.enable()` from `/resume_all`; default `OrderManager._enabled=False` | Telegram is a cash arming switch | **Small** | Do not touch `generate_paper_trade_signal`'s status guard. |
| 4 | `auto_promote` diagnose-only; delete/flag `_phase_refresh_signals`; add the falsifying test | Silent promotion laundering | **Small–medium** | Highest-leverage correctness fix. |
| 5 | `asyncio.to_thread` for portfolio sim + `detect_regime`; debounce health-file write | Event-loop freeze | **Small** | Bound `_PORTFOLIO_SIM_CACHE` (TTL-only today). |
| 6 | Indexes: deferred_work, hypotheses.status, paper_trades(sport,date,result) | Table scans as data grows | **Trivial** | |
| 7 | Hard-gate `/admin/restart`; refuse `force=true` → `live` | Local kill + gate bypass | **Trivial** | |
| 8 | One Kelly: `kelly_from_fair_prob(p, decimal_odds)` only; attic the other | Silent mis-size if live is ever armed | **Medium** | Characterization tests first. |
| 9 | `escalate_with_ladder` → `ProviderRouter.complete` adapter; then delete `MODEL_LADDER` | Two routers will drift until something pages at 3am | **Medium–large** | Do not point the kernel at Hermes CLI as the primary. Keep proxy-ahead-of-CLI. |
| 10 | Split `ResearchLoop` into phase modules; ban `except Exception: pass` in phases | Unattended blindness | **Large, incremental** | One phase per PR. Start with live-execute and evaluate. |
| 11 | Split `api.py` into routers + `AppState` | Every feature adds a global | **Large, incremental** | Start by extracting `routers/admin.py` and `routers/bets.py`. |
| 12 | Embeddings: stop N×N cluster; stop full-collection load | OOM on wiki growth | **Medium** | |
| 13 | SSRF allowlist on `RestSource.get` / wayback; path-constrain `joblib.load` | Classic RCE/SSRF once research is reachable | **Small** | |
| 14 | Attic: root junk files, `_dbg_*.py`, unread `minimum_events` writer, sport-priority fake-fix | False confidence | **Trivial** | Quarantine, don't delete, per mandate — except 0-byte glob wreckage, which is not code. |

**Do not do:** accept `status=='live'` in `generate_paper_trade_signal`. That is the ROADMAP loaded gun. It remains loaded.

---

## What is real, what is a worse copy, what finished would be

**Real (hard to download):** enforced confidence *floors* wired into schema checks; a promotion-gate *concept* with Šidák/Brier/CLV; a learning-layer seal that actually refuses unkeyed forgeries; integrated Kelly + drawdown + CLV + order approval as one (buggy) wiring. The scraped odds archive on the workstation, if it exists — not in this checkout.

**Worse copy of existing things:** sports scraping vs The Odds API; a hand-rolled event bus with one subscriber; a "plugin" registry the orchestrator does not call; ProviderRouter sitting next to `MODEL_LADDER` like a spare engine on the hangar floor.

**Finished:** one process kernel, one router, one Kelly, keyed seals that fail closed, loopback-by-default launchers, gates that cannot rewrite evidence, an event loop that only awaits, phases that can fail. Then — and only then — a conversation about paper-to-live.

---

## Handoff

Checklist in the operator prompt is complete. Next work is `ORCHESTRATION_HANDOFF.md` § First actions: independent review of `codex/checkpoint-trace-fidelity` (`dbcc751`) and `codex/run-persistence-unique-id` (`1ec9778`). Do not merge either on worker testimony. Do not dispatch OX workers until Nous Portal auth exists on the runner.
