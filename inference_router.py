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
"""

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from inference_kernel import _parse_json_response, logger


async def _post_with_retry(post_fn, endpoint: "EndpointConfig", payload: dict,
                           timeout: float, attempts: int = 2) -> tuple[str, dict]:
    """Retry transient failures within one endpoint before failing over.
    Connection errors and 5xx retry; other HTTP errors do not.

    SPEED run 8 (2026-08-23): upstream 429 (rate/capacity) also retries
    in place. Measured live: the ox_alpha proxy serves the SAME model as
    every later failover tier, but a Portal-capacity 429 is transient —
    failing over on it discarded the ~10x persistent-proxy win and landed
    every such call on the ~12-20s fresh-fork CLI path. Retry-in-place
    changes only WHERE the identical completion is served; non-429 4xx
    still fail over immediately and exhaustion still propagates to the
    existing failover chain. A Retry-After header is honoured, capped at
    _429_RETRY_AFTER_CAP_S so a hostile/lazy server cannot stall a call.
    """
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        slept = False
        try:
            return await post_fn(endpoint, payload, timeout)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status < 500 and status != 429:
                raise
            last_exc = e
            if status == 429:
                retry_after = _retry_after_seconds(e.response)
                if retry_after > _429_MAX_TOTAL_WAIT_S:
                    raise  # server says: back off longer than we may wait
                await _asyncio.sleep(retry_after)
                slept = True
        except (httpx.TransportError,) as e:
            last_exc = e
        if i < attempts - 1 and not slept:
            await _asyncio.sleep(0.5 * (i + 1))
    assert last_exc is not None
    raise last_exc


# ── SPEED run 8: 429 retry-in-place constants ─────────────────────────────
# A 429 with no Retry-After waits this long before the next in-place attempt.
_429_DEFAULT_BACKOFF_S = 1.0
# Never sleep longer than this on a Retry-After; a server demanding more
# backoff than we may spend fails over instead of stalling the caller.
_429_MAX_TOTAL_WAIT_S = 10.0


def _retry_after_seconds(response: httpx.Response) -> float:
    """Retry-After from a 429 response, in seconds, capped.

    Accepts delta-seconds (and ignores HTTP-date form — treat as default
    backoff rather than parsing dates). Missing/garbled header -> default.
    """
    raw = ""
    try:
        raw = response.headers.get("Retry-After") or ""
    except Exception:
        return _429_DEFAULT_BACKOFF_S
    try:
        val = float(raw.strip())
    except (ValueError, AttributeError):
        return _429_DEFAULT_BACKOFF_S
    if val < 0:
        return _429_DEFAULT_BACKOFF_S
    return min(val, _429_MAX_TOTAL_WAIT_S)


import asyncio as _asyncio
import time as _time
import yaml as _yaml
from pathlib import Path as _Path

_PROVIDERS_CONFIG_PATH = _Path(
    os.getenv("CALLISTO_PROVIDERS_CONFIG")
    or str(_Path(__file__).parent / "config" / "providers.yaml")
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
    cfg_path = _Path(path or _PROVIDERS_CONFIG_PATH)
    with open(cfg_path) as f:
        return _yaml.safe_load(f)


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


class _EndpointState:
    """Mutable runtime state for one endpoint: health, load, queue slot."""
    __slots__ = ("cfg", "semaphore", "consecutive_failures",
                 "cooldown_until", "in_flight")

    def __init__(self, cfg: EndpointConfig):
        self.cfg = cfg
        self.semaphore = _asyncio.Semaphore(cfg.max_concurrency)
        self.consecutive_failures = 0
        self.cooldown_until = 0.0
        self.in_flight = 0

    @property
    def available(self) -> bool:
        return (
            not self.cfg.extra.get("_unresolved")
            and _time.monotonic() >= self.cooldown_until
        )

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        # Exponential cooldown: 2s, 4s, 8s... capped at 60s.
        delay = min(60.0, 2.0 * (2 ** (self.consecutive_failures - 1)))
        self.cooldown_until = _time.monotonic() + delay


class CostLedger:
    """Tracks token usage + USD cost per tier. Hosted calls are budgeted;
    local calls are free at the margin and show up as $0."""

    def __init__(self, budget_usd: Optional[float] = None):
        self.budget_usd = budget_usd
        self.total_cost_usd = 0.0
        self.by_tier: dict = {}
        self._lock = _asyncio.Lock()

    async def record(self, tier: str, input_tokens: int,
                     output_tokens: int, cost_usd: float) -> None:
        async with self._lock:
            self.total_cost_usd += cost_usd
            t = self.by_tier.setdefault(
                tier, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                       "cost_usd": 0.0}
            )
            t["calls"] += 1
            t["input_tokens"] += input_tokens
            t["output_tokens"] += output_tokens
            t["cost_usd"] += cost_usd

    def snapshot(self) -> dict:
        return {
            "budget_usd": self.budget_usd,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "remaining_usd": (
                None if self.budget_usd is None
                else round(self.budget_usd - self.total_cost_usd, 6)
            ),
            "over_budget": (
                self.budget_usd is not None
                and self.total_cost_usd > self.budget_usd
            ),
            "by_tier": {
                k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                for k, v in sorted(self.by_tier.items())
            },
        }


class ProviderRouter:
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

    def _candidates_as_models(self, names: list[str]) -> list:
        from tools.routing.policy import CandidateModel
        out = []
        seen_identities: set[str] = set()
        for rank, n in enumerate(names):
            ep = self.endpoints.get(n)
            if ep is None:
                continue
            model_name = self.scoring_model_name(n)
            # Dedupe ONLY rails with an explicit canonical model identity.
            # Identity-less endpoints keep legacy standalone behaviour even
            # when their display `model` labels collide.
            if ep.model_identity:
                if ep.model_identity in seen_identities:
                    # Same canonical model via another transport rail: ONE
                    # scoring candidate, not several.
                    continue
                seen_identities.add(ep.model_identity)
            out.append(CandidateModel(
                name=model_name,
                tier=n,
                cost_per_1k_input=ep.cost_per_1k_input,
                cost_per_1k_output=ep.cost_per_1k_output,
                config_rank=rank,
            ))
        return out

    def route_order(self, task_class: str,
                    candidate_names: list[str],
                    role: Optional[str] = None) -> tuple[list[str], dict]:
        """Apply the empirical policy to one candidate list.

        Returns (reordered_names, honesty_metadata). With empirical routing
        disabled or zero measurements anywhere for this role, returns
        (candidate_names unchanged, {"basis": "configured"}) — exact
        degradation to today's configured behaviour.
        """
        meta: dict = {"basis": "configured", "role": role or task_class}
        if not self.empirical_routing_enabled or len(candidate_names) < 2:
            return candidate_names, meta
        try:
            from tools.routing.policy import ThompsonRoutingPolicy
            if self._routing_policy is None:
                self._routing_policy = ThompsonRoutingPolicy(
                    store=self.score_store,
                    cost_weight=self.empirical_cost_weight,
                    usd_per_brier_point=self.empirical_usd_per_brier_point)
            cands = self._candidates_as_models(candidate_names)
            if not cands:
                return candidate_names, meta
            decision = self._routing_policy.decide(role or task_class, cands)
        except Exception as e:  # never let measurement break a live call
            logger.warning(f"Empirical routing failed ({e}) — using config order")
            return candidate_names, {**meta, "error": str(e)}
        meta.update({
            "basis": decision.basis,
            "chosen_model": decision.model,
            "sampled_effective_loss": decision.sampled_effective_loss,
            "scores": decision.scores_used,
        })
        winner_identity: Optional[str] = None
        tier_ep = self.endpoints.get(decision.tier)
        if tier_ep is not None and tier_ep.model_identity:
            winner_identity = tier_ep.model_identity

        def _is_winner_rail(n: str) -> bool:
            if n == decision.tier:
                return True
            if winner_identity is None:
                return False
            ep = self.endpoints.get(n)
            return ep is not None and ep.model_identity == winner_identity

        if any(_is_winner_rail(n) for n in candidate_names):
            # Chosen model's ENTIRE rail group moves to the front as one
            # contiguous block (configured order preserved), so a proxy/CLI
            # failover pair is never separated. The rest keep their failover
            # order so a dead winner still degrades exactly as before.
            winners = [n for n in candidate_names if _is_winner_rail(n)]
            rest = [n for n in candidate_names if not _is_winner_rail(n)]
            return winners + rest, meta
        return candidate_names, meta

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

    def _group_by_identity(self, names: list[str]) -> list[str]:
        """Collapse rails that share a canonical model identity so the same
        physical model is ONE candidate, not several. The first-declared rail
        keeps its position (preserving configured transport priority); later
        rails of the same identity move to directly after it as failovers.
        Endpoints WITHOUT model_identity keep legacy per-endpoint behaviour."""
        if len(names) < 2:
            return names
        # group index per identity / standalone endpoint, assigned at FIRST
        # appearance in the configured order.
        group_of: dict[str, int] = {}  # identity -> group index
        standalone_group: dict[str, int] = {}  # endpoint -> group index
        next_group = 0
        for n in names:
            ident = self.endpoints[n].model_identity if n in self.endpoints else None
            if ident is None:
                standalone_group[n] = next_group
                next_group += 1
            elif ident not in group_of:
                group_of[ident] = next_group
                next_group += 1
        # Stable sort by group index preserves configured order WITHIN every
        # group while keeping each identity contiguous at its first
        # appearance; no-identity endpoints remain standalone.
        return sorted(names, key=lambda n: (
            group_of[self.endpoints[n].model_identity]
            if n in self.endpoints and self.endpoints[n].model_identity
            else standalone_group[n]))


    def scoring_model_name(self, endpoint_name: str) -> str:
        """Canonical name to record/lookup in the score store for an endpoint.
        Rails sharing a model identity share one scoring candidate; without
        an identity the display model label is used (legacy behaviour)."""
        ep = self.endpoints.get(endpoint_name)
        if ep is not None and ep.model_identity:
            return ep.model_identity
        return ep.model if ep is not None else endpoint_name

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
