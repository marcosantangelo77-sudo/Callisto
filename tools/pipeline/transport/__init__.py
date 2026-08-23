"""Persistent-transport backends for the Hermes/Ox Alpha model path.

Why this exists: `hermes -z` costs ~7-10s of process startup + agent build
per call, and a retrodiction question makes ~15 calls — ~2 minutes per
question paid to process boot alone (43-minute questions measured 2026-08-22).
This package holds warm agents alive and amortizes that cost to zero across
calls.

Backends, in preference order:

1. AgentPoolTransport ("agent_pool") — imports run_agent.AIAgent directly
   from the local Hermes install (the same library `hermes -z` wraps) and
   keeps a small pool of warm agents. Auth resolves through Hermes' own
   runtime-provider chain (keychain OAuth), so no credential is ever read,
   copied, or stored here. Per-call cost drops to roughly inference time.

   Why not `hermes serve`? Measured on this machine (2026-08-23): the serve
   gateway's JSON-RPC surface has no synchronous completion RPC. prompt.submit
   returns {"status": "streaming"} and delivers text via gateway events
   (message.delta / message.complete) keyed by a UI session id; WS auth is
   token/ticket-gated even on loopback; sessions are capped by a cross-process
   lease file shared with the desktop/TUI; and every submit runs a full agent
   turn (tools, memory injection, auto-title) rather than a stateless
   completion. Driving it means scraping a UI event stream over an
   authenticated socket — fragile in exactly the ways a transport must not be.
   The in-process pool gets the same win (warm agent) with none of that.

2. SubprocessTransport ("subprocess") — the original one-process-per-call
   path, kept as fallback when Hermes cannot be imported (wrong venv, missing
   install). Loud about being slow.

Both satisfy the same contract as hermes_cli.hermes_complete /
HermesCliModel.complete: flatten messages -> one completion ->
{"content": str}. Nothing downstream changes.

Concurrency honesty: Nous returns HTTP 429 past roughly 4-8 concurrent
requests on this account (measured; 8 parallel -> 429s, 4 parallel clean).
The pool size IS the concurrency ceiling — there are never more in-flight
model calls than agents — so backpressure is structural, not configured.
"""

from tools.pipeline.transport.agent_pool import (
    AgentPoolTransport,
    SubprocessTransport,
    get_shared_pool,
    reset_shared_pool,
)

__all__ = [
    "AgentPoolTransport",
    "SubprocessTransport",
    "get_shared_pool",
    "reset_shared_pool",
]
