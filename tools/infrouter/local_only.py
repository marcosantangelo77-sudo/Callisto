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
from urllib.parse import urlparse

from inference_kernel import logger

from tools.infrouter.config import EndpointConfig

LOCAL_BACKENDS = ("llama_cpp_server", "local")

# Substrings that make a URL (or extra.model) hosted even when backend
# claims llama_cpp_server / local. Do NOT scan the backend name — that
# would false-positive openai_compat on the token "openai".
HOSTED_URL_MARKERS = (
    "openrouter",
    "nousresearch",
    "anthropic.com",
    "api.openai.com",
    "openai.com",
    "ox-alpha",
    "ox_alpha",
)

# extra.provider values that are hosted control planes, not local boxes.
HOSTED_PROVIDER_NAMES = frozenset({
    "openrouter",
    "nous",
    "nousresearch",
    "anthropic",
    "openai",
})


def local_only_enabled() -> bool:
    """True when CALLISTO_LOCAL_ONLY is set to 1/true/yes."""
    return os.getenv("CALLISTO_LOCAL_ONLY", "").strip().lower() in (
        "1", "true", "yes")


def _haystack_parts(ep) -> tuple[str, str, str]:
    url = (getattr(ep, "base_url", None) or "").strip().lower()
    extra = getattr(ep, "extra", None) or {}
    provider = ""
    extra_model = ""
    if isinstance(extra, dict):
        provider = str(extra.get("provider") or "").strip().lower()
        extra_model = str(extra.get("model") or "").strip().lower()
    return url, provider, extra_model


def _url_looks_hosted(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed.hostname or "").lower()
    netloc = (parsed.netloc or "").lower()
    blob = f"{url} {host} {netloc}"
    return any(marker in blob for marker in HOSTED_URL_MARKERS)


def endpoint_is_hosted(ep: Optional[EndpointConfig]) -> bool:
    """Fail-closed hosted classification for one endpoint.

    Anything that is not an explicitly local backend counts as hosted.
    Local backends whose base_url / extra.provider / extra.model name a
    hosted host (openrouter, nous, anthropic, openai.com, ox-alpha) also
    count as hosted — a llama_cpp_server pointed at OpenRouter must not
    sneak through CALLISTO_LOCAL_ONLY.
    """
    if ep is None:
        return True
    backend = (ep.backend or "").strip().lower()
    if backend not in LOCAL_BACKENDS:
        return True
    url, provider, extra_model = _haystack_parts(ep)
    if provider in HOSTED_PROVIDER_NAMES:
        return True
    if _url_looks_hosted(url):
        return True
    if extra_model and any(m in extra_model for m in HOSTED_URL_MARKERS):
        return True
    return False


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
