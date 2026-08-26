"""
Ollama inference adapter for Callisto — facade.

Replaces the Hermes transformers layer with Ollama API calls.
Supports dual-mode tool call extraction: native Ollama tool_calls + Hermes XML fallback.

SPLIT (2026-08-26): this module re-exports TWO INFERENCE PLANES from their
own modules. Do not unify or delete either one:

1. KERNEL plane — inference_kernel.py: MODEL_LADDER (task_type -> ordered
   model list) plus the complete()/escalate_with_ladder() walk, the
   OllamaInference client, and tool-call plumbing.
2. CLI/pipeline plane — inference_router.py: ProviderRouter backed by
   config/providers.yaml via load_providers_config; endpoint-pool routing
   used by callisto.py.

Canonical FUTURE routing is ProviderRouter, but MODEL_LADDER must NOT be
pointed at it until a deliberate migration lands: measured Hermes CLI fork
latency is p50 ≈ 11.9s / max ≈ 31.4s (findings/hermes_latency_2026-08-26.md).
tests/test_inference_planes.py pins both planes.
"""

import httpx  # noqa: F401  # re-exported for back-compat (tests patch inference.httpx)

from inference_kernel import (  # noqa: F401
    AGENT_CONFIGS,
    AgentConfig,
    APRIEL_MODEL,
    DEVSTRAL_MODEL,
    FUNCTION_REGISTRY,
    GEMMA4_MODEL,
    MODEL_LADDER,
    OLLAMA_HOST,
    OllamaInference,
    PRELOAD_MODELS,
    QWEN36_MODEL,
    _HYPOTHESIS_REQUIRED_KEYS,
    _ET_OFFSET_HOURS,
    _claude_hours_window,
    _current_et_hour,
    _demote_claude_in_ladder,
    _extract_hermes_tool_calls,
    _get_hermes_validator,
    _get_inference,
    _in_claude_hours,
    _inference_cache,
    _make_agent,
    _parse_json_response,
    _register_default_tools,
    _validate_hypothesis_gen_output,
    execute_function_call,
    escalate_with_ladder,
    get_architect,
    get_manager,
    get_sentinel,
    logger,
    register_function,
    warmup_models,
)

from inference_router import (  # noqa: F401
    TASK_CLASS_ALIASES,
    CostLedger,
    EndpointConfig,
    EscalationConfig,
    ProviderRouter,
    TierConfig,
    UnknownTaskClassError,
    _429_DEFAULT_BACKOFF_S,
    _429_MAX_TOTAL_WAIT_S,
    _endpoint_from_config,
    _post_with_retry,
    _PROVIDERS_CONFIG_PATH,
    _retry_after_seconds,
    _router,
    get_router,
    load_providers_config,
)
