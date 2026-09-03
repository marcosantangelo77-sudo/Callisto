"""
Ollama inference adapter for Callisto (kernel plane).

Replaces the Hermes transformers layer with Ollama API calls.
Supports dual-mode tool call extraction: native Ollama tool_calls + Hermes XML fallback.

This module holds the KERNEL inference plane: MODEL_LADDER and the
complete()/escalate_with_ladder() walk, plus the OllamaInference client.
ProviderRouter lives in inference_router.py (CLI/pipeline plane) — see the
TWO INFERENCE PLANES note below; neither plane may be unified or deleted.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger("callisto.inference")

import httpx
from dotenv import load_dotenv

# Hermes path setup — imports are lazy to avoid pulling in pandas/yfinance at startup
# QUARANTINE (P4, 2026-08-22): the hermes-function-calling submodule moved to
# attic/ (see attic/hermes-function-calling.README.md). The old _HERMES_PATH
# sys.path insert and _get_hermes_tools() lazy import are GONE — they had zero
# call sites and the path insert would shadow top-level module names if the
# submodule were ever checked out. The live validator is the vendored
# tools/hermes_validator.py.

def _get_hermes_validator():
    """Vendored validator — see tools/hermes_validator.py for the verdict on
    why this no longer imports the upstream submodule's validator.py."""
    from tools.hermes_validator import validate_function_call_schema
    return validate_function_call_schema

load_dotenv(override=True)

# OLLAMA_HOST may be set system-wide as a bind address (e.g. 0.0.0.0).
# For client connections, always use localhost unless explicitly configured with a scheme.
_raw_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
if _raw_host in ("0.0.0.0", ":11434", "0.0.0.0:11434"):
    OLLAMA_HOST = "http://localhost:11434"
elif not _raw_host.startswith("http"):
    OLLAMA_HOST = f"http://{_raw_host}"
else:
    OLLAMA_HOST = _raw_host


@dataclass
class AgentConfig:
    model: str
    capabilities: list[str] = field(default_factory=list)
    default_options: dict = field(default_factory=dict)
    system_prompt: str = ""
    think: Optional[bool] = None  # None = model default, False = suppress thinking
    supports_native_tools: bool = False  # True = pass tools= to Ollama API


# VRAM budget: RTX 5060 Ti 16GB
# Context sizes and batch tuned per-agent role to maximize tp/s and minimize VRAM spill.
# num_ctx/num_batch/num_predict set in Modelfiles — only override temperature here.
# Flash attention enabled at Ollama server level (OLLAMA_FLASH_ATTENTION=1).
# Thinking suppressed on all agents — raw tp/s, no wasted tokens.
#
# Model lineup (March 2026):
#   Architect: Devstral Small 2 24B — SWE-bench leader, native tool use, fits 16GB VRAM
#   Manager:   GPT-OSS 20B (MXFP4) — fast, reliable adversarial review
#   Sentinel:  Qwen3.5 4B — ultra-fast classification, 3GB vs 9GB DeepSeek-R1
#
# Fallback ladder: When Claude is rate-limited, tasks fall through
# quality tiers instead of waiting. Task-type routing sends each task
# to the most capable available model for that specific capability.
AGENT_CONFIGS: dict[str, AgentConfig] = {
    "architect": AgentConfig(
        model="devstral-small-2",
        capabilities=["reasoning", "synthesis", "code_generation", "tool_use"],
        default_options={"temperature": 0.1},
        think=False,
        supports_native_tools=True,  # Devstral: SWE-bench leader, native function calling
        system_prompt=(
            "You are The Architect — the primary reasoning agent in the Callisto system. "
            "You handle complex analysis, code generation, and architecture decisions. "
            "You operate under the Aluft Gianne Protocol. "
            "Always respond with structured JSON when requested. Be concise."
        ),
    ),
    "manager": AgentConfig(
        model="manager:latest",
        capabilities=["review", "routing", "domain_enforcement", "tool_use"],
        default_options={"temperature": 0.4},
        think=False,
        supports_native_tools=True,  # GPT-OSS template has tool handling
        system_prompt="",  # set via Modelfile SYSTEM
    ),
    "strategist": AgentConfig(
        model="qwen36",
        capabilities=["reasoning", "synthesis", "hypothesis_gen", "deep_work"],
        default_options={"temperature": 0.7},  # Higher temp for creative hypothesis gen
        think=False,
        supports_native_tools=True,  # Qwen3 family: native tool/JSON via ChatML
        system_prompt=(
            "You are The Strategist — the creative reasoning agent in the Callisto system. "
            "You generate novel, diverse sports betting hypotheses and perform deep analysis. "
            "Focus on edges that models don't have columns for: team identity, roster sociology, "
            "ref biases, scheme geometry, media narrative inflation, calendar quirks. "
            "Always respond with structured JSON when requested. Be creative but rigorous."
        ),
    ),
    "sentinel": AgentConfig(
        model="qwen3.5:4b",
        capabilities=["classification", "monitoring", "domain_tagging"],
        default_options={"temperature": 0.0},
        think=False,
        system_prompt=(
            "You are The Sentinel — the classification agent in the Callisto system. "
            "Classify queries into exactly one domain: FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, or GENERAL. "
            "Respond ONLY with JSON."
        ),
    ),
}

# ── Model Fallback Ladder ──
# Task-type-aware routing with graceful degradation.
# Each task type has an ordered list of models to try.
# Claude Code is always first for reasoning/code tasks when available.
# Local models provide zero-downtime fallback at lower quality tiers.
#
# Quality tiers map to AGP source class clamping:
#   "frontier" = PRIMARY capable (Claude Code)
#   "high"     = SECONDARY max (strong local models)
#   "medium"   = SIGNAL max (fast local models)
# Installed models (March 2026, RTX 5060 Ti 16GB):
#   devstral-small-2           15GB (24B, agentic coding, SWE-bench leader)
#   Apriel-1.6-15B-Thinker    ~10GB (88% AIME, strongest reasoning local)
#   nemotron-cascade-2:latest  24GB (MoE 30B, 3B active, partial CPU offload)
#   gpt-oss:20b / manager      13GB (fits VRAM, 140 tok/s, matches o3-mini)
#   qwen3:14b                   9GB (matches 32B quality, thinking mode toggle)
#   deepseek-r1:14b             9GB (chain-of-thought reasoning)
#   qwen3.5:4b                3.4GB (ultra-fast classification)
#
# Devstral Small 2 24B: Purpose-built agentic coding model from Mistral.
#   - Best-in-class tool use at 16GB VRAM (SWE-bench, BFCL leader)
#   - Native function calling, no reasoning field quirks
#   - Same VRAM as mistral-small:24b but dramatically better at structured tasks
#
# Apriel-1.6-15B-Thinker: 88% AIME, 63.5% BFCL tool use. Produces
# chain-of-thought reasoning before JSON. _parse_json_response handles
# this by extracting the first JSON object from the response body.
# Stronger than GPT-OSS and Qwen3 on reasoning. Use for deep_work,
# hypothesis_gen, and reasoning tasks — NOT classification (too verbose).
# Qwen3-14B remains best for fast structured output — clean JSON first try.
DEVSTRAL_MODEL = "devstral-small-2"
APRIEL_MODEL = "hf.co/ServiceNow-AI/Apriel-1.6-15b-Thinker-GGUF:Q4_K_M"
GEMMA4_MODEL = "gemma4"  # E4B: 4.5B effective, 9.6GB. Demoted to fallback after qwen36 install.
# Qwen3.6-35B-A3B (April 2026, Unsloth UD-IQ3_S quant via local Modelfile alias).
# Sparse MoE: 35B total / 3B active per token. Beats Gemma 4-31B on SWE-bench
# Verified (73.4% vs 52.0%) and Terminal-Bench (51.5 vs 42.9), 86% GPQA, 92.7%
# AIME 2026. Local smoke test: ~50 tok/s steady-state, fits 16GB VRAM with KV
# cache headroom, clean JSON discipline. Replaces gemma4 as the primary local
# reasoning/hypothesis-gen/deep-work brain; gemma4 stays as one rung below.
QWEN36_MODEL = "qwen36"
# ---------------------------------------------------------------------------
# TWO INFERENCE PLANES — do not unify or delete either one in drive-by refactors.
#
# 1. MODEL_LADDER below (task_type -> ordered model list) is the kernel plane:
#    it is LIVE and is what inference.complete() walks for every call.
#    (Post-split it lives here, inference_kernel.py; inference.py re-exports.)
# 2. ProviderRouter + config/providers.yaml (loaded via load_providers_config)
#    is the CLI/pipeline plane: endpoint-pool routing used by callisto.py.
#    (Post-split it lives in inference_router.py.)
#
# Canonical FUTURE routing is ProviderRouter; a later PR may point the kernel
# at it — but only after measuring Hermes CLI fork latency. Measured
# 2026-08-26 (findings/hermes_latency_2026-08-26.md): p50 ≈ 11.9s,
# max ≈ 31.4s — the historical ~14s median holds and the tail is worse.
# Until a deliberate migration lands, both planes coexist intentionally. See
# tests/test_inference_planes.py, which pins this duplication.
MODEL_LADDER: dict[str, list[dict]] = {
    "reasoning": [
        {"model": "claude_code", "quality": "frontier", "timeout": 180},
        {"model": QWEN36_MODEL, "quality": "high", "timeout": 150},           # PRIMARY — Qwen3.6-35B-A3B MoE, 86% GPQA, 92.7% AIME
        {"model": GEMMA4_MODEL, "quality": "high", "timeout": 120},           # Fallback — Gemma 4 E4B, native function calling
        {"model": DEVSTRAL_MODEL, "quality": "high", "timeout": 120},         # Best local tool use, agentic coding
        {"model": APRIEL_MODEL, "quality": "high", "timeout": 120},           # 88% AIME, strongest local reasoning
        {"model": "qwen3:14b", "quality": "high", "timeout": 90},            # Matches 32B, thinking mode
        {"model": "qwen3.5:4b", "quality": "medium", "timeout": 60},
    ],
    "classification": [
        {"model": "qwen3.5:4b", "quality": "medium", "timeout": 30},
    ],
    "review": [
        {"model": QWEN36_MODEL, "quality": "high", "timeout": 120},          # PRIMARY review — broader reasoning than gemma
        {"model": GEMMA4_MODEL, "quality": "high", "timeout": 90},
        {"model": "manager:latest", "quality": "high", "timeout": 60},
    ],
    "code_generation": [
        {"model": "claude_code", "quality": "frontier", "timeout": 180},
        {"model": QWEN36_MODEL, "quality": "high", "timeout": 150},           # 73.4% SWE-bench Verified — beats gemma4-31B (52%)
        {"model": DEVSTRAL_MODEL, "quality": "high", "timeout": 120},         # Purpose-built for code, SWE-bench leader (24B dense)
        {"model": GEMMA4_MODEL, "quality": "high", "timeout": 120},
        {"model": "qwen3:14b", "quality": "high", "timeout": 90},            # Good code + JSON
    ],
    "hypothesis_gen": [
        # HYBRID MODE: Qwen3.6 handles hypothesis gen to save Claude credits.
        # Claude's value is in interpretation/deep work, not mass generation.
        {"model": QWEN36_MODEL, "quality": "high", "timeout": 180},           # PRIMARY — best local creative reasoning
        {"model": GEMMA4_MODEL, "quality": "high", "timeout": 150},           # Fallback creative
        {"model": DEVSTRAL_MODEL, "quality": "high", "timeout": 120},         # Fallback structured output
        {"model": "claude_code", "quality": "frontier", "timeout": 180},      # Last resort — saves $$$
    ],
    "deep_work": [
        {"model": "claude_code", "quality": "frontier", "timeout": 180},
        {"model": QWEN36_MODEL, "quality": "high", "timeout": 180},           # PRIMARY local deep work — 92.7% AIME 2026
        {"model": GEMMA4_MODEL, "quality": "high", "timeout": 180},
        {"model": DEVSTRAL_MODEL, "quality": "high", "timeout": 150},         # Best local for agentic deep analysis
        {"model": APRIEL_MODEL, "quality": "high", "timeout": 150},           # Strongest local for deep reasoning
        {"model": "qwen3:14b", "quality": "high", "timeout": 120},           # Best local for diagnosis
    ],
}

# Cached OllamaInference instances (one per model, reused)
_inference_cache: dict[str, "OllamaInference"] = {}

# Models to preload into VRAM at startup (keep_alive=24h).
# Devstral (15GB) = architect + primary fallback. qwen3.5:4b (3.4GB) = sentinel.
# Total ~18.4GB > 16GB VRAM, but qwen3.5 is tiny enough to reload in <2s when needed.
# Key insight: keep devstral ALWAYS loaded to avoid 30-110s cold loads.
PRELOAD_MODELS = ["devstral-small-2"]


async def warmup_models():
    """Preload priority models into VRAM with keep_alive to prevent thrashing."""
    async with httpx.AsyncClient(timeout=120) as client:
        for model in PRELOAD_MODELS:
            try:
                # Send a minimal request with keep_alive to pin the model in VRAM
                resp = await client.post(
                    f"{OLLAMA_HOST}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "options": {"num_predict": 1},
                        "keep_alive": "24h",
                    },
                )
                if resp.status_code == 200:
                    logger.info(f"Warmup: {model} loaded into VRAM (keep_alive=24h)")
                else:
                    logger.warning(f"Warmup: {model} returned {resp.status_code}")
            except Exception as e:
                logger.warning(f"Warmup: {model} failed: {e}")


def _get_inference(model: str) -> "OllamaInference":
    """Get or create a cached OllamaInference instance for a model.

    LATE-BIND NOTE: escalate_with_ladder resolves this helper through the
    ``inference`` facade namespace when available, so existing tests that
    patch ``inference._get_inference`` keep working after the split.
    """
    if model not in _inference_cache:
        _inference_cache[model] = OllamaInference(AgentConfig(
            model=model,
            default_options={"temperature": 0.1},
            think=False,
        ))
    return _inference_cache[model]


def _resolve(name: str):
    """Late-bind a helper through the inference facade namespace.

    After the split, tests patch helpers on ``inference`` (the facade).
    Resolving through that namespace — falling back to this module — keeps
    those patches effective without changing any call sites.
    """
    import sys
    facade = sys.modules.get("inference")
    if facade is not None and name in vars(facade):
        return vars(facade)[name]
    return globals()[name]


# ── Time-of-day routing ─────────────────────────────────────────────────
# Claude Max subscription has a tight 5h weekly budget; the bulk of
# interactive Claude usage happens 8am-2pm ET per user workflow. Outside
# that window we demote Claude to the last rung of the ladder so the
# autonomous loop spends the credit-window where it matters most.
# Override: CALLISTO_CLAUDE_HOURS="8-14" (inclusive start, exclusive end,
# ET hours on a 24h clock). Set CALLISTO_CLAUDE_HOURS="*" to disable
# demotion (always keep Claude at its default ladder position).

# ET = UTC-5 (EST) / UTC-4 (EDT). We don't run zoneinfo for the stdlib
# cross-platform headache; fixed -5 offset is fine for a coarse gate
# since the window is 6h wide and DST only shifts by 1h.
_ET_OFFSET_HOURS = -5


def _current_et_hour() -> int:
    """Current hour in ET (0-23), using a fixed -5 UTC offset."""
    et = datetime.now(timezone.utc) + timedelta(hours=_ET_OFFSET_HOURS)
    return et.hour


def _claude_hours_window() -> Optional[tuple[int, int]]:
    """Parse CALLISTO_CLAUDE_HOURS env var.

    Returns (start_hour, end_hour) on inclusive-start / exclusive-end
    semantics, or None to disable demotion (always allow Claude).
    Default: (8, 14) == 8am-2pm ET.
    """
    raw = os.getenv("CALLISTO_CLAUDE_HOURS", "8-14").strip()
    if raw in ("*", "any", "always"):
        return None
    try:
        start_s, end_s = raw.split("-", 1)
        start, end = int(start_s), int(end_s)
        if 0 <= start <= 24 and 0 <= end <= 24 and start != end:
            return start, end
    except (ValueError, TypeError):
        pass
    logger.warning(
        f"Invalid CALLISTO_CLAUDE_HOURS={raw!r} — falling back to 8-14"
    )
    return 8, 14


def _in_claude_hours(now_et_hour: Optional[int] = None) -> bool:
    """True iff we are currently inside the Claude Max credit window."""
    window = _claude_hours_window()
    if window is None:
        return True
    start, end = window
    h = now_et_hour if now_et_hour is not None else _resolve("_current_et_hour")()
    if start < end:
        return start <= h < end
    # Wrap-around window (e.g. 22-6): late-night into morning.
    return h >= start or h < end


def _demote_claude_in_ladder(ladder: list[dict]) -> list[dict]:
    """
    Move every 'claude_code' rung to the end of the ladder, preserving
    the relative order of other rungs. If the ladder has ANY local
    alternative ahead of Claude after the move, Claude effectively
    becomes a fallback of last resort. Called when we're outside the
    Claude Max hours to preserve credits for interactive use.
    """
    non_claude = [c for c in ladder if c.get("model") != "claude_code"]
    claude_rungs = [c for c in ladder if c.get("model") == "claude_code"]
    if not claude_rungs:
        return ladder
    return non_claude + claude_rungs


# ── Bridge output validation for hypothesis_gen ─────────────────────────
# When hypothesis_gen is routed through the local CC bridge (or any
# local model) we expect a JSON list of hypothesis dicts with at least
# these keys. If the output doesn't match, we degrade the quality tier
# so the ladder can escalate instead of silently returning garbage.
_HYPOTHESIS_REQUIRED_KEYS = frozenset({"name", "market", "edge_logic", "min_signals"})


def _validate_hypothesis_gen_output(content: str) -> bool:
    """
    Loose shape check: content parses to a list of dicts, each with
    the required hypothesis keys. We don't enforce value types beyond
    that — the consumer does richer validation downstream. A single
    well-formed dict (not wrapped in a list) also passes, to match
    historical tolerance.
    """
    parsed = _parse_json_response(content)
    if parsed is None:
        return False
    items = parsed if isinstance(parsed, list) else [parsed]
    if not items:
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        if not _HYPOTHESIS_REQUIRED_KEYS.issubset(item.keys()):
            return False
    return True


async def escalate_with_ladder(
    prompt: str,
    system_context: str = "",
    task_type: str = "reasoning",
    timeout: Optional[int] = None,
    **kwargs: Any,
) -> dict:
    """
    Try models in quality order for a given task type.

    Returns first successful result with metadata about which model was used.
    Claude Code is attempted first when available; local models provide fallback.
    Outside the Claude Max hours window (CALLISTO_CLAUDE_HOURS, default 8-14 ET),
    Claude is demoted to the last rung so the subscription budget is preserved
    for interactive use.

    Args:
        prompt: the task prompt
        system_context: optional system context (for Claude or system prompt)
        task_type: one of MODEL_LADDER keys (reasoning, classification, review, etc.)
        timeout: override timeout (uses ladder default if None)
        **kwargs: forward-compat extras. Recognized:
            - hermes_caller: passed through to claude_code_query (default "default")

    Returns:
        dict with keys: content, model_used, quality, ladder_step, error (if all failed)
    """
    from tools.claude_code import claude_code_query, is_available as claude_available

    hermes_caller = kwargs.get("hermes_caller", "default")

    ladder = MODEL_LADDER.get(task_type, MODEL_LADDER["reasoning"])
    # Preserve Claude credits outside the Max hours window.
    if not _in_claude_hours():
        demoted = _demote_claude_in_ladder(ladder)
        if demoted is not ladder:
            logger.debug(
                f"Ladder: outside Claude hours (ET hour={_current_et_hour()}) — "
                f"Claude demoted to last rung for task_type={task_type}"
            )
            ladder = demoted

    # ── Local CC bridge (CALLISTO_LOCAL_ONLY only) ──
    # When the nuclear kill switch is on, Claude paths are dead but we
    # still want a real tool-using agent for tasks that benefit from
    # multi-step reasoning. Try the forked-CC-driven-by-Ollama bridge
    # FIRST; on any failure (missing binary, timeout, non-zero exit,
    # empty output) fall THROUGH to the existing direct-Ollama ladder.
    # This is purely additive — the ladder below is untouched.
    try:
        from tools.local_cc_bridge import should_use_bridge, arun_local_cc

        if should_use_bridge(task_type):
            bridge_timeout_ms = (timeout or 900) * 1000  # default 15 min
            logger.info(
                f"Local-only mode: attempting local CC bridge for task_type={task_type}"
            )
            bridge_res = await arun_local_cc(
                prompt,
                system_context=system_context,
                timeout_ms=bridge_timeout_ms,
            )
            bridge_content = bridge_res.get("content", "")
            if bridge_content and not bridge_res.get("error"):
                # Shape-validate hypothesis_gen output before accepting.
                # On malformed schema, mark quality=low and fall through
                # so the ladder can escalate instead of shipping garbage.
                if task_type == "hypothesis_gen" and not _validate_hypothesis_gen_output(bridge_content):
                    logger.warning(
                        "Local CC bridge returned hypothesis_gen output that "
                        "does not match the required schema "
                        f"{sorted(_HYPOTHESIS_REQUIRED_KEYS)} — escalating"
                    )
                    # Fall through to ladder rather than returning low-quality.
                else:
                    logger.info(
                        f"Local CC bridge succeeded for task_type={task_type} "
                        f"(model={bridge_res.get('model_used')}, "
                        f"{len(bridge_content)} chars) — skipping direct Ollama ladder"
                    )
                    return {
                        "content": bridge_content,
                        "model_used": bridge_res.get("model_used", "local_cc"),
                        "quality": bridge_res.get("quality", "high"),
                        "ladder_step": -2,  # sentinel: bridge path, pre-ladder
                        "path": "local_cc_bridge",
                    }
            logger.info(
                f"Local CC bridge unavailable / failed "
                f"(error={bridge_res.get('error')!r}, "
                f"timed_out={bridge_res.get('timed_out')}) — "
                f"falling back to direct Ollama ladder"
            )
    except Exception as e:
        # Bridge import / dispatch should never break the ladder.
        logger.warning(f"Local CC bridge dispatch error: {e} — using direct ladder")

    for step, config in enumerate(ladder):
        model = config["model"]
        model_timeout = timeout or config["timeout"]

        try:
            if model == "claude_code":
                # Fail-closed: never walk the hosted Claude rung under
                # CALLISTO_LOCAL_ONLY. Skip this copy of the walk only —
                # do not mutate MODEL_LADDER (tests pin that
                # _demote_claude_in_ladder keeps the rung present).
                # claude_available() also returns False, but a patched
                # True must still not spawn Claude.
                if os.getenv("CALLISTO_LOCAL_ONLY", "").strip().lower() in (
                        "1", "true", "yes"):
                    logger.debug(
                        f"Ladder step {step}: Claude skipped "
                        f"(CALLISTO_LOCAL_ONLY)"
                    )
                    continue
                if not claude_available():
                    logger.debug(f"Ladder step {step}: Claude unavailable, skipping")
                    continue
                result = await claude_code_query(
                    prompt,
                    system_context,
                    timeout=model_timeout,
                    hermes_caller=hermes_caller,
                )
                content = result.get("content", "")
                if content and not result.get("error"):
                    # Schema-validate hypothesis_gen output before accepting.
                    if task_type == "hypothesis_gen" and not _validate_hypothesis_gen_output(content):
                        logger.warning(
                            f"Ladder step {step}: Claude hypothesis_gen output "
                            f"failed schema check — falling through"
                        )
                        continue
                    logger.info(f"Ladder step {step}: Claude Code succeeded")
                    return {
                        "content": content,
                        "model_used": "claude_code",
                        "quality": config["quality"],
                        "ladder_step": step,
                        "raw": result,
                    }
                logger.warning(f"Ladder step {step}: Claude returned error or empty")
                continue

            # Local Ollama model
            agent = _resolve("_get_inference")(model)
            messages = []
            if system_context:
                messages.append({"role": "system", "content": system_context})
            messages.append({"role": "user", "content": prompt})


            response = await agent.achat(
                messages, options={"num_predict": 2048}
            )
            content = response.get("content", "")
            if content:
                # Schema-validate hypothesis_gen output from local models too.
                if task_type == "hypothesis_gen" and not _validate_hypothesis_gen_output(content):
                    logger.warning(
                        f"Ladder step {step}: {model} hypothesis_gen output "
                        f"failed schema check — falling through"
                    )
                    continue
                logger.info(f"Ladder step {step}: {model} succeeded ({len(content)} chars)")
                return {
                    "content": content,
                    "model_used": model,
                    "quality": config["quality"],
                    "ladder_step": step,
                }
            logger.warning(f"Ladder step {step}: {model} returned empty")

        except Exception as e:
            logger.warning(f"Ladder step {step}: {model} failed: {e}")
            continue

    logger.error(f"All models exhausted for task_type={task_type}")
    return {
        "content": "",
        "model_used": "none",
        "quality": "none",
        "ladder_step": -1,
        "error": "All models in ladder exhausted",
    }


# Function registry for tool execution
FUNCTION_REGISTRY: dict[str, callable] = {}


def register_function(name: str, func: callable) -> None:
    """Register a function for tool call execution."""
    FUNCTION_REGISTRY[name] = func


def _register_default_tools() -> None:
    """Register built-in tools on import."""
    from tools.brave_search import brave_search_sync
    register_function("brave_search", brave_search_sync)  # legacy compat
    register_function("web_search", brave_search_sync)

    from tools.claude_code import claude_code_sync
    register_function("claude_code", claude_code_sync)

    from tools.odds_api import (
        get_sports as _get_sports,
        get_odds as _get_odds,
        get_scores as _get_scores,
        get_event_odds as _get_event_odds,
        find_best_line as _find_best_line,
        calculate_ev as _calculate_ev,
        get_credit_status as _get_credit_status,
    )
    # Sync wrappers for the async odds functions
    import asyncio as _aio

    def _sync_get_sports():
        return _aio.get_event_loop().run_until_complete(_get_sports())

    def _sync_get_odds(**kwargs):
        return _aio.get_event_loop().run_until_complete(_get_odds(**kwargs))

    def _sync_get_scores(**kwargs):
        return _aio.get_event_loop().run_until_complete(_get_scores(**kwargs))

    def _sync_get_event_odds(**kwargs):
        return _aio.get_event_loop().run_until_complete(_get_event_odds(**kwargs))

    register_function("get_sports", _sync_get_sports)
    register_function("get_odds", _sync_get_odds)
    register_function("get_scores", _sync_get_scores)
    register_function("get_event_odds", _sync_get_event_odds)
    register_function("find_best_line", _find_best_line)
    register_function("calculate_ev", _calculate_ev)
    register_function("get_credit_status", _get_credit_status)

    # Line gap analysis
    from tools.line_gaps import scan_line_gaps, scan_prop_gaps
    register_function("scan_line_gaps", scan_line_gaps)
    register_function("scan_prop_gaps", scan_prop_gaps)

    # Boost evaluator
    from tools.boost_evaluator import (
        devig_multiplicative as _devig_mult,
        evaluate_fixed_boost as _eval_fixed,
        evaluate_percentage_boost as _eval_pct,
        evaluate_free_bet as _eval_free,
        calculate_hedge as _calc_hedge,
        find_optimal_boost_target as _find_optimal,
    )
    register_function("devig_multiplicative", _devig_mult)
    register_function("evaluate_fixed_boost", _eval_fixed)
    register_function("evaluate_percentage_boost", _eval_pct)
    register_function("evaluate_free_bet", _eval_free)
    register_function("calculate_hedge", _calc_hedge)
    register_function("find_optimal_boost_target", _find_optimal)


_register_default_tools()


def _extract_hermes_tool_calls(text: str) -> list[dict]:
    """Extract tool calls from Hermes-style <tool_call> XML blocks."""
    calls = []
    pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
    for match in re.finditer(pattern, text, re.DOTALL):
        try:
            call_data = json.loads(match.group(1))
            calls.append(call_data)
        except json.JSONDecodeError:
            continue
    return calls


def _parse_json_response(text: str) -> Optional[Any]:
    """Try to parse JSON from model response, handling markdown fences."""
    text = text.strip()
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting from markdown code fences
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding first JSON object/array
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == start_char:
                depth += 1
            elif text[i] == end_char:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return None


class OllamaInference:
    """Ollama inference client using raw httpx HTTP calls.

    Replaces the ollama Python library which hangs indefinitely on some models
    (notably devstral-small-2). Raw httpx calls complete in ~3s for the same
    requests. Uses stream=false for single JSON responses.
    """

    # Default timeout for inference requests (seconds).
    # Most models in MODEL_LADDER use 60-150s; 180s covers the longest.
    _DEFAULT_TIMEOUT = 180

    def __init__(self, config: AgentConfig):
        self.config = config
        self.client = httpx.Client(
            base_url=OLLAMA_HOST,
            timeout=httpx.Timeout(self._DEFAULT_TIMEOUT, connect=10.0),
        )
        self.async_client = httpx.AsyncClient(
            base_url=OLLAMA_HOST,
            timeout=httpx.Timeout(self._DEFAULT_TIMEOUT, connect=10.0),
        )

    def _build_chat_payload(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        options: Optional[dict] = None,
        think: Optional[bool] = None,
        format: Optional[dict] = None,
    ) -> dict:
        """Build the JSON payload for /api/chat."""
        opts = {**self.config.default_options, **(options or {})}
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "options": opts,
            "stream": False,
        }
        if tools and self.config.supports_native_tools:
            payload["tools"] = tools
        # think parameter: explicit arg > config default
        t = think if think is not None else self.config.think
        if t is not None:
            payload["think"] = t
        if format is not None:
            payload["format"] = format
        return payload

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        options: Optional[dict] = None,
        think: Optional[bool] = None,
    ) -> dict:
        """Synchronous chat with the model via POST /api/chat."""
        payload = self._build_chat_payload(messages, tools, options, think)
        resp = self.client.post("/api/chat", json=payload)
        resp.raise_for_status()
        return self._process_response(resp.json())

    async def achat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        options: Optional[dict] = None,
        think: Optional[bool] = None,
        format: Optional[dict] = None,
    ) -> dict:
        """Async chat with the model via POST /api/chat.

        Args:
            format: JSON schema dict for Ollama structured output.
                    When provided, output is constrained to match the schema exactly.
        """
        payload = self._build_chat_payload(messages, tools, options, think, format)
        resp = await self.async_client.post("/api/chat", json=payload)
        resp.raise_for_status()
        return self._process_response(resp.json())

    def generate(self, prompt: str, options: Optional[dict] = None) -> str:
        """Raw generate (completion mode) via POST /api/generate."""
        opts = {**self.config.default_options, **(options or {})}
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "options": opts,
            "stream": False,
        }
        resp = self.client.post("/api/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "") or ""

    def ping(self) -> dict:
        """Check if the model is responsive. Returns status dict."""
        try:
            payload = {
                "model": self.config.model,
                "messages": [{"role": "user", "content": "ping"}],
                "options": {"num_predict": 1},
                "stream": False,
            }
            resp = self.client.post("/api/chat", json=payload)
            resp.raise_for_status()
            return {"status": "ok", "model": self.config.model}
        except Exception as e:
            return {"status": "error", "model": self.config.model, "error": str(e)}

    async def aping(self) -> dict:
        """Async version of ping."""
        try:
            payload = {
                "model": self.config.model,
                "messages": [{"role": "user", "content": "ping"}],
                "options": {"num_predict": 1},
                "stream": False,
            }
            resp = await self.async_client.post("/api/chat", json=payload)
            resp.raise_for_status()
            return {"status": "ok", "model": self.config.model}
        except Exception as e:
            return {"status": "error", "model": self.config.model, "error": str(e)}

    def _process_response(self, response: dict) -> dict:
        """Process raw dict response from Ollama HTTP API.

        Handles dual-mode tool call extraction:
        1. Native Ollama tool_calls from the response JSON
        2. Hermes XML fallback (<tool_call>...</tool_call>) in content
        """
        message = response.get("message", {})

        content = message.get("content", "") or ""
        thinking = message.get("thinking", "") or ""

        # If content is empty but thinking has content, use thinking as content
        # (DeepSeek-R1 puts everything in thinking when think=True)
        if not content and thinking:
            content = thinking

        tool_calls = []

        # Mode 1: Native Ollama tool_calls
        native_calls = message.get("tool_calls")
        if native_calls:
            for tc in native_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", {})
                tool_calls.append({"name": name, "arguments": args})

        # Mode 2: Hermes XML fallback (if no native calls found)
        if not tool_calls and content:
            hermes_calls = _extract_hermes_tool_calls(content)
            for hc in hermes_calls:
                tool_calls.append({
                    "name": hc.get("name", ""),
                    "arguments": hc.get("arguments", {}),
                })

        # Parse JSON from content if present
        parsed_json = _parse_json_response(content) if content else None

        return {
            "content": content,
            "tool_calls": tool_calls,
            "parsed_json": parsed_json,
            "raw": response,
        }


def execute_function_call(name: str, arguments: dict) -> Any:
    """Execute a registered function by name."""
    if name not in FUNCTION_REGISTRY:
        return {"error": f"Unknown function: {name}"}
    try:
        return FUNCTION_REGISTRY[name](**arguments)
    except Exception as e:
        logger.error(f"Function call {name}({arguments}) failed: {e}", exc_info=True)
        return {"error": f"Function {name} failed: {str(e)}"}


def _make_agent(name: str) -> OllamaInference:
    config = AGENT_CONFIGS[name]
    inference = OllamaInference(config)
    return inference


def get_architect() -> OllamaInference:
    return _make_agent("architect")


def get_manager() -> OllamaInference:
    return _make_agent("manager")


def get_sentinel() -> OllamaInference:
    return _make_agent("sentinel")
