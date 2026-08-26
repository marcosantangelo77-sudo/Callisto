"""ProviderRouter internals extracted from inference_router.py.

The CLI/pipeline inference plane stays on ``inference_router.ProviderRouter``.
This package holds config loading, 429 retry-in-place, the CALLISTO_LOCAL_ONLY
hosted strip, and endpoint runtime state.

Do NOT point MODEL_LADDER at ProviderRouter. Completions stay HTTP.
Hermes is the agent runtime, not a kernel-plane transport.
"""

from tools.infrouter.config import (
    TASK_CLASS_ALIASES,
    EndpointConfig,
    EscalationConfig,
    TierConfig,
    UnknownTaskClassError,
    _PROVIDERS_CONFIG_PATH,
    _endpoint_from_config,
    load_providers_config,
)
from tools.infrouter.local_only import (
    LOCAL_BACKENDS,
    endpoint_is_hosted,
    local_only_enabled,
    strip_hosted_for_local_only,
)
from tools.infrouter.retry import (
    _429_DEFAULT_BACKOFF_S,
    _429_MAX_TOTAL_WAIT_S,
    _post_with_retry,
    _retry_after_seconds,
)
from tools.infrouter.state import CostLedger, _EndpointState

__all__ = [
    "TASK_CLASS_ALIASES",
    "EndpointConfig",
    "EscalationConfig",
    "TierConfig",
    "UnknownTaskClassError",
    "_PROVIDERS_CONFIG_PATH",
    "_endpoint_from_config",
    "load_providers_config",
    "LOCAL_BACKENDS",
    "endpoint_is_hosted",
    "local_only_enabled",
    "strip_hosted_for_local_only",
    "_429_DEFAULT_BACKOFF_S",
    "_429_MAX_TOTAL_WAIT_S",
    "_post_with_retry",
    "_retry_after_seconds",
    "CostLedger",
    "_EndpointState",
]
