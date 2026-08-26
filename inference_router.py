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
endpoint state, empirical reordering). This module keeps the facade class
and complete() dispatch. CALLISTO_LOCAL_ONLY fail-closed hosted strip runs
in candidates_for()/tier_for() BEFORE any complete() dispatch.
"""

from typing import Any, Optional

import asyncio as _asyncio
import time as _time

import httpx

from inference_kernel import _parse_json_response, logger

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
        """Process/router-wide pooled AsyncClient. Rebuilt if the running
        event loop changed (asyncio transports are loop-bound). A client that
        does not expose ``is_closed`` (test doubles) is treated as spent, so
        opaque stand-ins keep the legacy fresh-client-per-call shape."""
        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        current = self._http_client
        spent = (current is None
                 or bool(getattr(current, "is_closed", True))
                 or getattr(current, "_bound_loop", None) is not loop)
        if spent:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=10.0),
                limits=httpx.Limits(
                    max_connections=32,
                    max_keepalive_connections=8,
                    keepalive_expiry=120.0,
                ),
            )
            client._bound_loop = loop  # type: ignore[attr-defined]
            self._http_client = client
        return self._http_client

    def _reset_shared_client(self) -> None:
        self._http_client = None

    async def aclose(self) -> None:
        """Close the shared pool (graceful shutdown / tests)."""
        client = self._http_client
        self._http_client = None
        if client is not None and not getattr(client, "is_closed", True):
            await client.aclose()

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
        ep = self.endpoints[name]
        if ep.backend == "hermes_cli":
            # No HTTP to probe: healthy iff the binary resolves. A real ping
            # would burn a ~14s CLI session per health pass.
            from tools.pipeline.hermes_cli import hermes_available
            if hermes_available():
                self.states[name].record_success()
                return {"endpoint": name, "status": "ok"}
            self.states[name].record_failure()
            return {"endpoint": name, "status": "error",
                    "error": "hermes CLI binary not found"}
        headers = {"Content-Type": "application/json"}
        if ep.api_key:
            headers["Authorization"] = f"Bearer {ep.api_key}"
        payload = {
            "model": ep.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        try:
            client = self._shared_client()
            resp = await client.post(
                f"{ep.base_url}/chat/completions", json=payload,
                headers=headers, timeout=timeout,
            )
            resp.raise_for_status()
            self.states[name].record_success()
            return {"endpoint": name, "status": "ok"}
        except Exception as e:
            self.states[name].record_failure()
            return {"endpoint": name, "status": "error", "error": str(e)}

    async def health_report(self) -> dict:
        results = await _asyncio.gather(
            *(self.check_health(n) for n in self.endpoints),
            return_exceptions=True,
        )
        out = {}
        for name, r in zip(self.endpoints, results):
            out[name] = r if isinstance(r, dict) else {
                "endpoint": name, "status": "error", "error": repr(r)}
        return out

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
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "messages": messages,
            "temperature": (
                temperature if temperature is not None else endpoint.temperature
            ),
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if schema is not None:
            # Structured output. llama-server supports json_schema in
            # response_format; hosted OpenAI-compat APIs accept it too.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "callisto_output", "schema": schema},
            }
        return payload

    @staticmethod
    def _tier_alias_for_compat(name: str, raw: dict) -> EndpointConfig:
        return _endpoint_from_config(name, raw)

    async def _post(self, endpoint: EndpointConfig, payload: dict,
                    timeout: float) -> tuple[str, dict]:
        headers = {"Content-Type": "application/json"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        # SPEED run 2026-08-23: one shared AsyncClient (connection pool) instead
        # of a fresh client per request. A fresh client pays TCP connect + TLS
        # handshake every call — measured ~0.3s extra per call against a remote
        # TLS host, on top of inference time, for every completion and health
        # probe. The shared client reuses pooled keep-alive connections.
        # Per-request timeout still overrides the client default; failover
        # semantics are unchanged (errors propagate to _post_with_retry).
        client = self._shared_client()
        resp = await client.post(
            f"{endpoint.base_url}/chat/completions", json=payload,
            headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            logger.warning(
                f"ProviderRouter: malformed completion response from "
                f"endpoint {endpoint.name}: keys={list(data)}"
            )
            content = ""
        usage = data.get("usage") or {}
        return content, usage

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
        """One routed completion, with failover across the tier pool,
        per-endpoint concurrency limiting, and cost accounting.

        `role` (W2): when set AND empirical routing is enabled with measured
        scores, the measured per-(role, model) record reorders the candidate
        list. The returned dict carries "routing_basis" so every caller can
        see whether this decision was measured or merely configured.

        Returns {"content", "parsed_json", "model", "tier", "task_class",
                 "routing_basis"}.
        Raises only when EVERY candidate endpoint failed (or none can serve
        the requested capability) — a dead endpoint degrades, never crashes.
        """
        msgs = self.build_messages(messages, system_context)
        errors: list[str] = []

        base_candidates = self.candidates_for(task_class, schema=schema)
        ordered, routing_meta = self.route_order(
            task_class, base_candidates, role=role)

        for name in ordered:
            endpoint = self.endpoints[name]
            state = self.states[name]

            if endpoint.cost_per_1k_input or endpoint.cost_per_1k_output:
                if (self.budget_usd is not None
                        and self.cost_ledger.total_cost_usd >= self.budget_usd
                        and not allow_budget_exceed):
                    errors.append(
                        f"{name}: budget ${self.budget_usd:.2f} exhausted "
                        f"(spent ${self.cost_ledger.total_cost_usd:.2f}) — "
                        f"refusing paid tier; pass allow_budget_exceed=True "
                        f"to override deliberately"
                    )
                    continue
            payload = self._payload(endpoint, msgs, schema, temperature, max_tokens) \
                if endpoint.backend != "hermes_cli" else None
            queued_at = _time.monotonic()
            try:
                # Backpressure: wait here if the endpoint is saturated.
                async with state.semaphore:
                    queue_s = _time.monotonic() - queued_at
                    state.in_flight += 1
                    try:
                        if endpoint.backend == "hermes_cli":
                            from tools.pipeline.hermes_cli import (
                                hermes_complete,
                                _default_max_procs as _hc_procs)
                            if _hc_procs() < self.endpoints[name].max_concurrency:
                                logger.warning(
                                    f"ProviderRouter: hermes_cli endpoint "
                                    f"{name} declares max_concurrency="
                                    f"{self.endpoints[name].max_concurrency} "
                                    f"> CALLISTO_HERMES_MAX_PROCS — the "
                                    f"shared process semaphore will bound "
                                    f"forks to {_hc_procs()}")
                            res = await hermes_complete(
                                msgs,
                                role=str(task_class),
                                timeout_s=float(timeout),
                                # Bind the explicitly configured provider/
                                # model as the CLI routing target (mirrors
                                # the supervisor's --provider/-m); absent
                                # fields mean no flag is passed.
                                provider=endpoint.extra.get("provider"),
                                model=endpoint.extra.get("model"),
                            )
                            content = res["content"]
                            usage: dict = {}
                        else:
                            content, usage = await _post_with_retry(
                                self._post, endpoint, payload, timeout
                            )
                    finally:
                        state.in_flight -= 1
                state.record_success()

                in_tok = int(usage.get("prompt_tokens", 0) or 0)
                out_tok = int(usage.get("completion_tokens", 0) or 0)
                cost = (
                    in_tok / 1000 * endpoint.cost_per_1k_input
                    + out_tok / 1000 * endpoint.cost_per_1k_output
                )
                await self.cost_ledger.record(name, in_tok, out_tok, cost)

                if queue_s > 1.0:
                    logger.info(
                        f"ProviderRouter: {name} was saturated — queued "
                        f"{queue_s:.1f}s for task_class={task_class}"
                    )
                return {
                    "content": content,
                    "parsed_json": _parse_json_response(content) if content else None,
                    "model": endpoint.model,
                    "tier": name,
                    "task_class": task_class,
                    "routing_basis": routing_meta.get("basis", "configured"),
                }
            except Exception as e:
                state.record_failure()
                errors.append(f"{name}: {e}")
                logger.warning(
                    f"ProviderRouter: endpoint {name} failed "
                    f"({state.consecutive_failures} consecutive) — failing over: {e}"
                )

        raise RuntimeError(
            f"All endpoints failed for task_class={task_class!r}: "
            f"{'; '.join(errors) or 'no candidates'}"
        )

    def complete_sync(self, *args, **kwargs) -> dict:
        """Synchronous wrapper around complete()."""
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("complete_sync() called from inside a running loop")
        return _asyncio.run(self.complete(*args, **kwargs))

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
