"""providers.yaml load + endpoint dataclasses for ProviderRouter.

Path resolution is anchored at the repo root (parents[2] of this file),
matching the historical ``inference_router.py`` location next to
``config/providers.yaml``. Override with CALLISTO_PROVIDERS_CONFIG.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVIDERS_CONFIG_PATH = Path(
    os.getenv("CALLISTO_PROVIDERS_CONFIG")
    or str(_REPO_ROOT / "config" / "providers.yaml")
)


class UnknownTaskClassError(KeyError):
    """Raised when complete() gets a task_class not declared in providers.yaml.

    LOUD by design: a typo'd task_class must never silently fall back to the
    default tier — that is how routing decisions stop being decisions.
    """


# ── Vocabulary bridge ──────────────────────────────────────────────────────
# The codebase (tools/autonomous.py, MODEL_LADDER keys) passes these names;
# providers.yaml historically declared different ones. The ROUTER side is
# authoritative: call-site names are accepted as aliases of canonical task
# classes so routing works before instance 1's rename pass lands.
TASK_CLASS_ALIASES: dict[str, str] = {
    # call-site name -> canonical task class
    "deep_work": "research_synthesis",
    "hypothesis_gen": "hypothesis_generation",
    "reasoning": "research_synthesis",
    "review": "adversarial_review",
    "code_generation": "research_synthesis",
}


@dataclass(frozen=True)
class EndpointConfig:
    """One model server process. A 'tier' may be served by MANY endpoints
    (e.g. two GPU boxes running llama-server); routing picks among them."""
    name: str
    backend: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    context_tokens: int = 32768
    temperature: float = 0.2
    vram_gb: float = 0.0                    # informational / placement hints
    structured_output: bool = True          # json_schema response_format OK?
    tool_calls: bool = False                # native function calling?
    max_concurrency: int = 1                # parallel in-flight requests
    cost_per_1k_input: float = 0.0          # USD; local = 0.0
    cost_per_1k_output: float = 0.0
    # Stable canonical identity of the served model (e.g.
    # "nous/stealth/ox-alpha"). Endpoints sharing an identity are ONE model
    # choice for scoring/routing — different transports of the same weights.
    # Absent => each endpoint stands alone (legacy behaviour).
    model_identity: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TierConfig:
    """Back-compat view: tier name -> ordered candidate endpoints."""
    name: str
    backend: str
    base_url: str                           # first endpoint (compat)
    model: str                              # first endpoint (compat)
    api_key: Optional[str] = None
    context_tokens: int = 32768
    temperature: float = 0.2
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EscalationConfig:
    json_schema_failures: int = 2
    tool_error_loops: int = 2
    confidence_below: Optional[float] = None


def load_providers_config(path=None) -> dict:
    cfg_path = Path(path or _PROVIDERS_CONFIG_PATH)
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def _endpoint_from_config(name: str, raw: dict) -> EndpointConfig:
    """Build an EndpointConfig from one entry under `providers:`.

    Env-backed fields (base_url_env / api_key_env / model_env) resolve at
    build time when set; if unset the endpoint is marked _unresolved and is
    skipped by routing (LOUD log) rather than crashing construction — that
    keeps a local-only box constructible while a hosted tier is configured.

    backend="hermes_cli" needs NEITHER base_url nor model: it shells out to
    the Hermes CLI (Nous Portal OAuth lives in the keychain) and serves the
    hosted stealth-ox-alpha model, so base_url stays "" and model defaults
    to "ox-alpha". Such an endpoint is never _unresolved.

    Routing target binding: extra.provider / extra.model (if configured) are
    passed to the CLI as --provider / -m before `-z`. Endpoints without them
    keep relying on external Hermes defaults (backward compatible).
    """
    backend = raw.get("backend", "openai_compat")
    base_url = raw.get("base_url")
    if not base_url and raw.get("base_url_env"):
        base_url = os.getenv(raw["base_url_env"], "")
        if not base_url:
            raw = {**raw, "_unresolved": True}
    api_key = None
    if raw.get("api_key_env"):
        api_key = os.getenv(raw["api_key_env"]) or None
    # Model resolution precedence: a NONEMPTY configured model_env value
    # overrides the static model; an unset or empty env value falls back to
    # the static model (which may itself be absent for env-only configs).
    model = raw.get("model")
    env_model: Optional[str] = None
    if raw.get("model_env"):
        env_model = os.getenv(raw["model_env"], "") or None
        if env_model:
            model = env_model
    if backend == "hermes_cli":
        # No URL, no env vars, no keychain access — just the binary.
        unresolved = False
        model = model or "ox-alpha"
        base_url = ""
    else:
        unresolved = bool(raw.get("_unresolved")) or not (base_url and model)
    # Canonical-identity safety rule: a static `model_identity` is only
    # trustworthy while the effective served model matches the configured
    # static one. A nonempty `model_env` override pointing at a DIFFERENT
    # model means we no longer know which weights actually run there, so the
    # declared identity is invalidated (the endpoint becomes standalone).
    # An explicit resolved identity may still be supplied via
    # `resolved_model_identity` / `resolved_model_identity_env`; it is NEVER
    # inferred from the override's model string.
    model_identity = raw.get("model_identity") or None
    if env_model and env_model != (raw.get("model") or None):
        model_identity = None
    resolved_identity = (
        os.getenv(raw["resolved_model_identity_env"], "") or None
        if raw.get("resolved_model_identity_env")
        else (raw.get("resolved_model_identity") or None))
    if resolved_identity:
        model_identity = resolved_identity

    return EndpointConfig(
        name=name,
        backend=raw.get("backend", "openai_compat"),
        base_url=(base_url or "").rstrip("/"),
        model=model or "",
        api_key=api_key,
        context_tokens=int(raw.get("context_tokens", 32768)),
        temperature=float(raw.get("temperature", 0.2)),
        vram_gb=float(raw.get("vram_gb", 0) or 0),
        structured_output=bool(raw.get("structured_output", True)),
        tool_calls=bool(raw.get("tool_calls", False)),
        max_concurrency=max(1, int(raw.get("max_concurrency", 1))),
        cost_per_1k_input=float(raw.get("cost_per_1k_input", 0) or 0),
        cost_per_1k_output=float(raw.get("cost_per_1k_output", 0) or 0),
        model_identity=model_identity,
        extra={**(raw.get("extra") or {}), **({"_unresolved": True} if unresolved else {})},
    )
