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
