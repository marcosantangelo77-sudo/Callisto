"""Routed completion dispatch extracted from ``ProviderRouter.complete``.

``async def complete(router, ...)`` is the CLI/pipeline inference plane:
task_class -> candidates_for (CALLISTO_LOCAL_ONLY strip) -> HTTP post with
in-place 429 retry, cost ledger, and failover. ``hermes_complete`` remains
a last-resort ``hermes_cli`` backend inside this function — not the agent
runtime, not a MODEL_LADDER transport.

Do not point MODEL_LADDER at ProviderRouter. Do not import
``tools.autonomous``. Completions stay HTTP.
"""
from __future__ import annotations

from typing import Optional

import asyncio as _asyncio
import time as _time

from inference_kernel import _parse_json_response, logger
from tools.infrouter.retry import _post_with_retry


async def complete(
    router,
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
    msgs = router.build_messages(messages, system_context)
    errors: list[str] = []

    base_candidates = router.candidates_for(task_class, schema=schema)
    ordered, routing_meta = router.route_order(
        task_class, base_candidates, role=role)

    for name in ordered:
        endpoint = router.endpoints[name]
        state = router.states[name]

        if endpoint.cost_per_1k_input or endpoint.cost_per_1k_output:
            if (router.budget_usd is not None
                    and router.cost_ledger.total_cost_usd >= router.budget_usd
                    and not allow_budget_exceed):
                errors.append(
                    f"{name}: budget ${router.budget_usd:.2f} exhausted "
                    f"(spent ${router.cost_ledger.total_cost_usd:.2f}) — "
                    f"refusing paid tier; pass allow_budget_exceed=True "
                    f"to override deliberately"
                )
                continue
        payload = router._payload(endpoint, msgs, schema, temperature, max_tokens) \
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
                        if _hc_procs() < router.endpoints[name].max_concurrency:
                            logger.warning(
                                f"ProviderRouter: hermes_cli endpoint "
                                f"{name} declares max_concurrency="
                                f"{router.endpoints[name].max_concurrency} "
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
                            router._post, endpoint, payload, timeout
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
            await router.cost_ledger.record(name, in_tok, out_tok, cost)

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


def complete_sync(router, *args, **kwargs) -> dict:
    """Synchronous wrapper around complete()."""
    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("complete_sync() called from inside a running loop")
    return _asyncio.run(router.complete(*args, **kwargs))
