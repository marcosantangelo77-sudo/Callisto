"""ProviderRouter — the CLI/pipeline inference plane (split from inference.py).

task_class -> endpoint POOL -> best capable endpoint.
Per config/providers.yaml. Adding compute (a second GPU box, a 3090/5090,
a DGX Spark alongside today's 5060 Ti) is a config entry — nothing else.

This is ONE of the TWO INFERENCE PLANES. The kernel plane (MODEL_LADDER +
the ladder walk) lives in inference_kernel.py and remains what
inference.complete()/escalate_with_ladder() walks. Do NOT unify the planes:
measured Hermes fork latency (findings/hermes_latency_2026-08-26.md,
p50 ≈ 11.9s / max ≈ 31.4s) does not support pointing MODEL_LADDER at this
router yet. See tests/test_inference_planes.py, which pins both planes.

Internals live in tools.infrouter (config, 429 retry, LOCAL_ONLY strip,
endpoint state, empirical reordering, shared HTTP pool, complete()).
This module keeps the facade class, candidates_for(), and thin
complete()/complete_sync() delegates. CALLISTO_LOCAL_ONLY fail-closed
hosted strip runs in candidates_for()/tier_for() BEFORE any complete()
dispatch.
"""

from typing import Any, Optional

import httpx

from inference_kernel import logger

from tools.infrouter.config import (  # noqa: F401
    TASK_CLASS_ALIASES,
    EndpointConfig,
    EscalationConfig,
    TierConfig,
    UnknownTaskClassError,
    _PROVIDERS_CONFIG_PATH,
    _endpoint_from_config,
    load_providers_config,
)
from tools.infrouter.empirical import EmpiricalRoutingMixin
from tools.infrouter.local_only import (  # noqa: F401
    LOCAL_BACKENDS,
    endpoint_is_hosted,
    local_only_enabled,
    strip_hosted_for_local_only,
)
from tools.infrouter.retry import (  # noqa: F401
    _429_DEFAULT_BACKOFF_S,
    _429_MAX_TOTAL_WAIT_S,
    _post_with_retry,
    _retry_after_seconds,
)
from tools.infrouter.state import CostLedger, _EndpointState
from tools.infrouter import complete as _complete, http as _http


class ProviderRouter(EmpiricalRoutingMixin):
    """Routes task_class -> tier -> best available endpoint in that tier.

    Usage at call sites:

        router.complete("research_synthesis", messages, schema=_SCHEMA)

    Call-site legacy names (deep_work, hypothesis_gen, reasoning, review,
    code_generation) are accepted via TASK_CLASS_ALIASES.

    Design contract (SCOPE CORRECTION 2026-08-22): adding compute is a
    config entry. A tier lists N endpoints; each declares capabilities
    (context window, structured output, tool calls), a concurrency limit,
    and unit costs. Routing picks the healthiest idle endpoint; dead ones
    cool down exponentially instead of crashing the loop.

    Budget: hosted endpoints declare $/1k tokens; every completion is
    charged to the ledger. With `routing.budget.usd` set, hosted tiers are
    refused once the budget is spent unless allow_budget_exceed=True —
    escalation to frontier must be deliberate, visible, budgeted.
    """

    def __init__(self, config_path=None):
        cfg = load_providers_config(config_path)
        self.default_tier_name = cfg.get("default_tier", "local")
        self._raw_providers = cfg.get("providers") or {}
        self.endpoints: dict[str, EndpointConfig] = {
            name: _endpoint_from_config(name, raw)
            for name, raw in self._raw_providers.items()
        }
        self.states: dict[str, _EndpointState] = {
            name: _EndpointState(ep) for name, ep in self.endpoints.items()
        }
        routing = cfg.get("routing") or {}

        # task_classes values may be ONE tier name (back-compat) or a list
        # of tier names in preference order (multi-tier fallback).
        self.task_classes: dict[str, Any] = routing.get("task_classes") or {}
        esc = routing.get("escalation") or {}
        self.escalation = EscalationConfig(
            json_schema_failures=int(esc.get("json_schema_failures", 2)),
            tool_error_loops=int(esc.get("tool_error_loops", 2)),
            confidence_below=(
                float(esc["confidence_below"]) if esc.get("confidence_below") else None
            ),
        )
        budget = (routing.get("budget") or {})
        self.budget_usd: Optional[float] = (
            float(budget["usd"]) if budget.get("usd") is not None else None
        )
        self.cost_ledger = CostLedger(budget_usd=self.budget_usd)
        self.health_checks_enabled = bool(
            (routing.get("health_checks") or {}).get("enabled", True)
        )

        # ── W2: empirical model routing ──
        # Measured per-(role, model) scores reorder the candidate list BEFORE
        # the configured order applies; with no measurements the policy
        # returns basis="configured" and behaviour is byte-identical to the
        # configured tier list. Nothing gets worse before measurements exist.
        emp = routing.get("empirical_routing") or {}
        self.empirical_routing_enabled = bool(emp.get("enabled", False))
        self.empirical_cost_weight = float(emp.get("cost_weight", 0.5))
        self.empirical_usd_per_brier_point = float(
            emp.get("usd_per_brier_point", 5.0))
        self._score_store = None
        self._routing_policy = None
        # Shared HTTP connection pool (speed run 2026-08-23). Created lazily,
        # bound to the first running event loop that uses it; a test can force
        # re-creation with _reset_shared_client() after changing loops.
        self._http_client: Optional[httpx.AsyncClient] = None

    def _shared_client(self) -> httpx.AsyncClient:
        return _http.shared_client(self)

    def _reset_shared_client(self) -> None:
        _http.reset_shared_client(self)

    async def aclose(self) -> None:
        """Close the shared pool (graceful shutdown / tests)."""
        await _http.aclose_client(self)

    @property
    def score_store(self):
        """Lazy ModelScoreStore so importing tools.routing stays off the hot
        construction path and tests can inject a tmp-path store."""
        if self._score_store is None:
            from tools.routing.scores import ModelScoreStore
            self._score_store = ModelScoreStore()
        return self._score_store

    @score_store.setter
    def score_store(self, store) -> None:
        self._score_store = store

    # ── vocabulary ──

    def canonical_task_class(self, task_class: str) -> str:
        tc = TASK_CLASS_ALIASES.get(task_class, task_class)
        if tc not in self.task_classes:
            raise UnknownTaskClassError(
                f"task_class {task_class!r} (canonical {tc!r}) not declared in "
                f"{_PROVIDERS_CONFIG_PATH}; declared: {sorted(self.task_classes)}"
            )
        return tc

    # ── back-compat surface ──

    def tiers_view_names(self) -> list[str]:
        """Names of configured endpoints, in declaration order."""
        return list(self.endpoints)

    def tier_for(self, task_class: str) -> TierConfig:
        """Resolve a task class to its FIRST usable tier (legacy shape).
        Unknown classes raise LOUDLY. Unresolved env-backed endpoints raise
        LOUDLY rather than falling back silently."""
        tc = self.canonical_task_class(task_class)
        names = self.task_classes[tc]
        if isinstance(names, str):
            names = [names]
        # CALLISTO_LOCAL_ONLY: tier_for must not hand back a hosted first
        # tier either. Strip hosted rails; if nothing local remains, raise
        # loudly (strip_hosted_for_local_only does that for us).
        names = strip_hosted_for_local_only(self, names, task_class)
        for n in names:
            ep = self.endpoints.get(n)
            if ep is None:
                continue
            if ep.extra.get("_unresolved"):
                raise RuntimeError(
                    f"tier endpoint '{n}' has no resolved base_url/model — "
                    f"set its *_env vars to use task class {task_class!r}"
                )
            return TierConfig(
                name=n, backend=ep.backend, base_url=ep.base_url,
                model=ep.model, api_key=ep.api_key,
                context_tokens=ep.context_tokens, temperature=ep.temperature,
                extra=ep.extra,
            )
        raise RuntimeError(f"task class {task_class!r} has no usable endpoints")

    # ── capability-based selection ──

    def candidates_for(self, task_class: str,
                       schema: Optional[dict] = None) -> list[str]:
        """Endpoint names for a task class, healthy-first, capability-ordered."""
        tc = self.canonical_task_class(task_class)
        names = self.task_classes[tc]
        if isinstance(names, str):
            names = [names]
        # CALLISTO_LOCAL_ONLY: fail-closed strip of hosted rails BEFORE any
        # health/availability logic, so hosted endpoints can never win —
        # not even as cooling-down fallbacks.
        names = strip_hosted_for_local_only(self, names, task_class)
        out = []
        for n in names:
            ep = self.endpoints.get(n)
            st = self.states.get(n)
            if st is None or ep is None:
                continue
            if not st.available:
                continue
            # hermes_cli declares structured_output=False honestly — it
            # cannot enforce a schema. It is still usable for schema-bearing
            # calls on a BEST-EFFORT basis (JSON-in-text + _parse_json_response),
            # which is what keeps a CLI-only laptop running the whole system.
            # Callers needing a hard guarantee must not rely on it: check
            # ep.structured_output themselves.
            if (schema is not None and not ep.structured_output
                    and ep.backend != "hermes_cli"):
                continue
            out.append(n)
        if not out:
            # Everything cooling down (or filtered): prefer least-bad rather
            # than raising — degrade, don't crash the loop.
            fallback = [n for n in names
                        if n in self.states
                        and not self.endpoints[n].extra.get("_unresolved")
                        and (schema is None
                             or self.endpoints[n].structured_output
                             or self.endpoints[n].backend == "hermes_cli")]
            if fallback:
                logger.warning(
                    f"All endpoints for task_class={task_class!r} cooling "
                    f"down; using {fallback[0]} anyway"
                )
                return self._group_by_identity(fallback)
        return self._group_by_identity(out)

    def pick_endpoint(self, task_class: str, schema: Optional[dict] = None,
                      tools: bool = False) -> Optional[EndpointConfig]:
        """Best available endpoint satisfying the request's capability needs.
        Prefers lowest current load, then declared order."""
        for name in self.candidates_for(task_class):
            ep = self.endpoints[name]
            if schema is not None and not ep.structured_output:
                continue
            if tools and not ep.tool_calls:
                continue
            return ep
        return None

    # ── health ──

    async def check_health(self, name: str, timeout: float = 5.0) -> dict:
        """Probe one endpoint with a minimal chat request."""
        return await _http.check_health(self, name, timeout)

    async def health_report(self) -> dict:
        return await _http.health_report(self)

    # ── completion ──

    @staticmethod
    def build_messages(messages: list[dict], system_context: str = "") -> list[dict]:
        out = []
        if system_context:
            out.append({"role": "system", "content": system_context})
        out.extend(messages)
        return out

    @staticmethod
    def _payload(
        endpoint: EndpointConfig,
        messages: list[dict],
        schema: Optional[dict],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> dict:
        return _http.build_payload(
            endpoint, messages, schema, temperature, max_tokens
        )

    @staticmethod
    def _tier_alias_for_compat(name: str, raw: dict) -> EndpointConfig:
        return _endpoint_from_config(name, raw)

    async def _post(self, endpoint: EndpointConfig, payload: dict,
                    timeout: float) -> tuple[str, dict]:
        return await _http.post(self, endpoint, payload, timeout)

    async def complete(
        self,
        task_class: str,
        messages: list[dict],
        schema: Optional[dict] = None,
        system_context: str = "",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: float = 300.0,
        allow_budget_exceed: bool = False,
        role: Optional[str] = None,
    ) -> dict:
        """One routed completion. Body: tools.infrouter.complete.

        Dispatches through candidates_for; hermes_cli is last-resort CLI.
        """
        return await _complete.complete(
            self,
            task_class,
            messages,
            schema=schema,
            system_context=system_context,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            allow_budget_exceed=allow_budget_exceed,
            role=role,
        )

    def complete_sync(self, *args, **kwargs) -> dict:
        """Synchronous wrapper around complete()."""
        return _complete.complete_sync(self, *args, **kwargs)

    def status(self) -> dict:
        """Expose routing + cost state (wire into GET /system/full-status)."""
        return {
            "default_tier": self.default_tier_name,
            "endpoints": {
                n: {
                    "base_url": self.endpoints[n].base_url,
                    "model": self.endpoints[n].model,
                    "max_concurrency": self.endpoints[n].max_concurrency,
                    "in_flight": self.states[n].in_flight,
                    "available": self.states[n].available,
                    "consecutive_failures": self.states[n].consecutive_failures,
                    "cost_per_1k_input": self.endpoints[n].cost_per_1k_input,
                    "cost_per_1k_output": self.endpoints[n].cost_per_1k_output,
                }
                for n in self.endpoints
            },
            "cost": self.cost_ledger.snapshot(),
        }


_router: Optional[ProviderRouter] = None


def get_router() -> ProviderRouter:
    """Process-wide router, loaded once. Set inference._router = None to reset."""
    global _router
    if _router is None:
        _router = ProviderRouter()
    return _router
