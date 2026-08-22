# Instance 5 — serving & inference (audit/tier5-serving)

## [VERIFIED] inference.py:849→1086 — ProviderRouter implemented, legacy surface untouched
Blast radius: LOUD
Evidence: `ProviderRouter` appended to inference.py (nothing removed). Routes
task_class -> tier -> OpenAI-compatible endpoint per config/providers.yaml.
Unknown task classes raise `UnknownTaskClassError` (a typo must never
silently fall to default tier — same failure shape as ROADMAP's inline-literal
finding, inverted defensively). Env-backed tiers (frontier) resolve lazily:
constructible local-only, raise LOUD at first use when FRONTIER_* unset.
24 new tests pass; tests/test_local_cc_bridge.py + test_hypgen_integration_smoke.py
(40 tests) still pass. Commit a4a8672.
Falsifier: an import-time regression in monitor.py/orchestrator.py/api.py
(warmup_models) would falsify "legacy surface untouched".
For: owned.

## [VERIFIED] hermes-function-calling/ — ROADMAP's "~200 lines" verdict: RIGHT in substance, overstated in size
Blast radius: SILENT (had it been left as-is)
Evidence: grep across repo — zero live imports of the submodule beyond
inference.py's two lazy helpers (`functions.get_openai_tools`,
`validator.validate_function_call_schema`), both currently dead call paths.
UPSTREAM.md import claims confirmed false (matches ROADMAP §2). The genuinely
live descendant is `_extract_hermes_tool_calls` (inference.py:603), which uses
json.loads ONLY — missing upstream utils.py's ast.literal_eval rescue that
recovers Python-literal tool calls from small models. Upstream validator.py:22
(`if call_arg_value:`) skips falsy args AND passes bools as ints
(get_python_type 'integer' -> int; isinstance(True, int) is True) — verified by
reading the pinned tree at ~/Documents/GitHub/Callisto/hermes-function-calling.
Actual live surface: ~120 lines, not ~200.
Action taken: tools/hermes_validator.py vendors exactly that surface —
validate_function_call_schema swapped to jsonschema (bool-as-int now rejected,
minimums/enums enforced), XML+literal_eval extraction ladder preserved with a
never-executes-code test. Submodule itself left pinned (quarantine rule;
restore note in the file docstring). inference._get_hermes_validator rewired
to it.
Falsifier: if a future model emits tool calls needing chat_templates/ or
prompter.py, the deletion verdict is wrong for those parts too.
For: owned (vendored copy); submodule attic decision unowned.

## [VERIFIED] config/providers.yaml hardware math — checked against Aug-2026 sources, holds
Blast radius: none (documentation)
Evidence: Qwen3.8-27B released 2026-08-14 (Apache 2.0, hybrid Gated DeltaNet,
256K native context) — huggingface.co/Qwen/Qwen3.8-27B and local-ai-zone
analysis confirm 24GB-minimum claims are for FP8/full quants; unsloth GGUF
repo shows UD-Q3_K_XL ≈13.4 GB → fits 16 GB VRAM resident with room for KV.
The DeltaNet claim (~64 KiB/token KV) is consistent with the 3:1 linear/full
attention ratio in the technical analysis. providers.yaml's llama-server
recommendation stands. One caveat INFERRED below.
Falsifier: a measured OOM or <10 tok/s on real Callisto prompts would falsify.
For: unowned (hardware).

## [INFERRED] Tiered routing split — providers.yaml mapping is right but incomplete; three task classes misfiled vs the actual codebase
Blast radius: MED (cost/quality tradeoff)
Evidence: providers.yaml declares 8 task_classes; the actual call sites use
DIFFERENT names — tools/autonomous.py passes task_type="deep_work" (6×),
"hypothesis_gen" (2×), plus MODEL_LADDER keys reasoning/review/classification/
code_generation (inference.py:165-206). None of deep_work / hypothesis_gen /
reasoning / review / code_generation exist in providers.yaml routing. The two
vocabularies do not meet anywhere — the router is live but nothing can reach
it without a rename pass (owned follow-up, deliberately NOT done in this
pass since every call site is in instance 1's files).
Substantive split recommendation (research-backed):
  - Grind (local_fast, 4B-class): classification/screening/extraction/
    domain tagging. Zero marginal cost, wrong-answer cost low, volume high.
  - Resident 27B (local): hypothesis_generation, research_synthesis,
    backtest_interpretation, deep_work, review. Qwen3.8-27B at Q3_K_XL
    beats Mistral-Small-class on agentic benchmarks; these tasks are
    frequent enough that frontier pricing compounds.
  - Frontier (promotion_judgment, adversarial_review only): few calls,
    money-consequential, and adversarial review specifically benefits from
    independence from the model being reviewed. Escalation triggers
    (schema-failure count, disagreement, confidence floor) are the correct
    mechanism; keep them.
The one place I'd disagree with the placeholder: `adversarial_review:
frontier` should be *conditional* — frontier for promotion-adjacent reviews,
local for ordinary self-critique, else the loop will burn credits reviewing
scrapes. Falsifier: log the task_class distribution for one week of autonomous
loop; if frontier calls exceed ~5% of completions, the escalation gates are
too loose.
For: instance 1 owns the call-site renames (tools/autonomous.py).

## [VERIFIED] api.py characterization (read-only for this instance)
Blast radius: n/a
Evidence: 114 @app endpoints over ~4,685 statements; thin glue over tools/*
plus background loops defined in-module (wal_checkpoint_loop :292,
restart_signal_watcher :362, ingestion_sla_watchdog_loop :410, task_worker
:660, order_cron_loop :771). Exactly ONE inference touchpoint:
`from inference import warmup_models` at api.py:871 inside lifespan — so the
router change cannot break any endpoint. Auth posture: require_admin /
require_admin_or_loopback dependencies present on all mutating routes I
sampled (orders, executor, admin/sql); public GET odds/wiki routes are
read-only. The 23.2% coverage gap lives almost entirely in the five
background loops and the long odds-analytics endpoints (parlay_scan :1690,
sgp_analysis :1763), not in the plumbing. No changes made to api.py.
Falsifier: coverage report showing api.py:871 region exercised elsewhere.
For: unowned.

## Deferred (deliberately)
- Renaming escalate_with_ladder call sites to router.complete(task_class=...):
  every site is in tools/autonomous.py + tools/hypothesis_generator.py —
  instance 1's files. The router is additive; migration can proceed per-site.
- Moving the submodule to attic/: quarantine rule says owner decides; the
  vendored replacement removes the need to touch it.

---

# Plug-and-play compute (2026-08-22, follow-up pass)

Commits: dadfb2d (characterization), 1794217 (pool router), f9e9e50 (pool
tests), 4c500fa (providers.yaml pool config).

## [VERIFIED] ProviderRouter is now an endpoint POOL with capability routing, health/failover, queueing, and a cost ledger
Blast radius: LOUD (legacy surface preserved)
Evidence: inference.py:853-1433. Each `providers:` entry is one endpoint
declaring vram_gb, context_tokens, structured_output, tool_calls,
max_concurrency, and $/1k-token costs. `routing.task_classes` values may be
one endpoint name (back-compat) or a LIST in failover order. complete()
walks candidates: skips cooling-down endpoints, filters by capability
(schema needs structured_output=true), enforces per-endpoint
asyncio.Semaphore(max_concurrency), retries transient errors in-place
(2 attempts), fails over on exhaustion, records tokens+USD in CostLedger.
Dead endpoints get exponential cooldown (2s→60s cap); if ALL candidates are
cooling it degrades to the first rather than crashing the loop — fan-out
against a dead box cannot take down the autonomous loop. 47 tier5 tests
pass (characterization + pool + original + validator).
Falsifier: run two concurrent complete() calls against max_concurrency=1
endpoint with a counter in the server — peak concurrency >1 falsifies the
semaphore claim. Unplug gpu1 and watch research_synthesis fail outright —
falsifies failover.
For: owned.

## [VERIFIED] Concurrency/backpressure — the fan-out thrash problem is closed
Blast radius: SILENT before, bounded now
Evidence: llama-server serves ONE request at a time unless launched with
--parallel N; orchestrator.py fans out to parallel agents. Before this
pass, N parallel agents would open N sockets into a 1-slot server —
queueing inside TCP, no visibility, timeout storms. Now each endpoint has
a declared max_concurrency and an asyncio.Semaphore; excess requests wait
in the router (backpressure), waits over 1s are logged, in_flight counts
are exposed via router.status() for /system/full-status wiring.
INFERRED caveat: default configs still declare max_concurrency: 1 — correct
for stock llama-server, but the OWNER must raise it in lockstep with
--parallel at launch. Mismatch direction (config > server) re-creates
thrash invisibly. Falsifier: benchmark tok/s at fan-out=4 vs serial on the
real box; a >30% drop falsifies "runs well".
For: config values unowned (owner's hardware).

## [VERIFIED] Cost & budget awareness — frontier escalation is now deliberate
Blast radius: LOW (additive)
Evidence: local endpoints cost $0/1k (free at margin); frontier declares
cost_per_1k_input/output. Every completion charges CostLedger from the
response usage block. routing.budget.usd (default $5) caps process-lifetime
hosted spend: once spent, paid endpoints REFUSE unless the caller passes
allow_budget_exceed=True — escalation becomes an explicit decision visible
at the call site. router.status()/CostLedger.snapshot() expose spend by
endpoint. Note: promotion_judgment/adversarial_review list [frontier, gpu1],
so budget exhaustion falls back LOCAL instead of blocking judgment calls —
degraded quality, never a crashed loop.
Falsifier: set budget.usd: 0.000001 and confirm promotion_judgment routes
to gpu1 without erroring; a hard failure would falsify.
For: owned.

## FOR INSTANCE 1 — exact rename list (router side made authoritative meanwhile)
The vocabulary gap is BRIDGED, not just documented: inference.TASK_CLASS_ALIASES
maps every call-site name to a canonical task class, so all existing call
sites route correctly TODAY. The renames below are for vocabulary hygiene,
not correctness. When instance 1 migrates escalate_with_ladder call sites
to router.complete(), use these:

  Call-site name (current)     -> canonical task class        | sites
  ---------------------------------------------------------------------
  task_type="deep_work"        -> research_synthesis          | autonomous.py ×6
  task_type="hypothesis_gen"   -> hypothesis_generation       | autonomous.py ×2 (+hypothesis_generator.py paths)
  task_type="reasoning"        -> research_synthesis          | MODEL_LADDER key, inference.py:167
  task_type="review"           -> adversarial_review          | MODEL_LADDER key, inference.py:179
  task_type="code_generation"  -> research_synthesis          | MODEL_LADDER key, inference.py:184

Migration contract: canonical names are exactly the keys of
routing.task_classes in providers.yaml (8 classes). If instance 1 wants a
DIFFERENT canonical name (e.g. keep "deep_work" as canonical), change the
alias map OR add the name to task_classes — both are one-line edits;
the router raises UnknownTaskClassError loudly on anything undeclared.
[VERIFIED] bridge works: tests assert deep_work/hypothesis_gen/reasoning/
review/code_generation all resolve against the real repo config.

## [VERIFIED] providers.yaml rewritten as endpoint pool with scaling recipes
Blast radius: none until owner adds hardware
Evidence: header documents the plug-and-play contract; recipes for second
box (3090), DGX Spark alongside, bigger single GPU (5090). Today's file
still describes exactly one 5060 Ti (gpu1) + grind endpoint + env-backed
frontier, and degrades to gpu1 alone. Frontier pricing ($3/$15 per 1k) is
PLACEHOLDER — flagged in-file for owner to edit to real numbers.
Falsifier: adding a `gpu2:` entry and appending it to two task_classes
lists should need zero .py edits — any code change falsifies the contract.
For: hardware entries unowned (owner); schema owned.

## Single-box degradation check [VERIFIED]
With only gpu1 alive: screening→gpu1_fast, everything local→gpu1,
frontier tasks→frontier if env vars resolve else LOUD RuntimeError at
tier_for (candidates_for skips unresolved endpoints, so complete() with a
local-only fallback list still works). Construction never requires
FRONTIER_* to be set — verified by test_missing_frontier_base_url_raises.
Falsifier: unset all FRONTIER_* env vars and import inference + construct
ProviderRouter — any exception falsifies.
