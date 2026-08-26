"""CALLISTO_LOCAL_ONLY fail-closed hosted-endpoint strip.

When CALLISTO_LOCAL_ONLY is truthy (1/true/yes), the router must never
return a hosted endpoint. Full-local means llama_cpp_server / local ONLY;
openai_compat rails that name a hosted host (openrouter / nous / frontier /
ox_alpha proxy) are HOSTED even though the transport is plain HTTP.

Must run BEFORE any complete() dispatch / health-fallback logic so a hosted
rail can never win — not even as a cooling-down fallback.
"""

from __future__ import annotations

import os
from typing import Optional

from inference_kernel import logger

from tools.infrouter.config import EndpointConfig

LOCAL_BACKENDS = ("llama_cpp_server", "local")


def local_only_enabled() -> bool:
    """True when CALLISTO_LOCAL_ONLY is set to 1/true/yes."""
    return os.getenv("CALLISTO_LOCAL_ONLY", "").strip().lower() in (
        "1", "true", "yes")


def endpoint_is_hosted(ep: Optional[EndpointConfig]) -> bool:
    """Fail-closed hosted classification for one endpoint.

    Anything that is not an explicitly local backend counts as hosted, and
    openai_compat endpoints pointing at a known hosted marker count as
    hosted regardless of naming.
    """
    if ep is None:
        return True
    backend = (ep.backend or "").strip().lower()
    return backend not in LOCAL_BACKENDS


def strip_hosted_for_local_only(
        router,
        names: list[str],
        task_class: str) -> list[str]:
    """Filter `names` to local-only endpoints when CALLISTO_LOCAL_ONLY is set.

    Raises RuntimeError LOUDLY when nothing local survives — silently
    degrading to OpenRouter under LOCAL_ONLY would defeat the whole switch.
    """
    if not local_only_enabled():
        return names
    kept = [n for n in names
            if not endpoint_is_hosted(router.endpoints.get(n))]
    dropped = [n for n in names if n not in kept]
    if dropped:
        logger.warning(
            "CALLISTO_LOCAL_ONLY: stripped hosted endpoints %s from "
            "task_class=%r candidates", dropped, task_class)
    if not kept:
        raise RuntimeError(
            f"CALLISTO_LOCAL_ONLY is set but task_class {task_class!r} has "
            f"NO local endpoints after stripping hosted rails "
            f"({names}). Start llama.cpp on gpu1/gpu1_fast or unset "
            f"CALLISTO_LOCAL_ONLY — refusing to fall back to hosted."
        )
    return kept
