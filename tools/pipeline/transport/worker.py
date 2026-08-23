#!/usr/bin/env python3
"""Warm-agent worker process for Callisto's Hermes transport.

One process hosts ONE warm Hermes AIAgent. The parent (Callisto) speaks
newline-delimited JSON over stdin/stdout:

    -> {"op": "complete", "id": "...", "role": "...",
        "messages": [...], "timeout_s": 240}
    <- {"id": "...", "ok": true, "content": "...", "elapsed_s": 1.2}
    <- {"id": "...", "ok": false, "error": "..."}

    -> {"op": "ping"}            /  <- {"ok": true, "pong": true}
    -> {"op": "shutdown"}        /  process exits

Why a subprocess rather than importing run_agent in-process: both Callisto
and Hermes ship a top-level ``tools`` package; importing both in one
interpreter is a namespace collision. A dedicated worker running under the
Hermes venv keeps each package intact while still paying agent startup once
per worker lifetime instead of once per call.

The worker NEVER prints anything except JSON frames on stdout — all Hermes
chatter goes to stderr or devnull.
"""
from __future__ import annotations

import json
import os
import sys
import time


def _build_agent(model: str):
    os.environ.setdefault("HERMES_YOLO_MODE", "1")
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent

    rt = resolve_runtime_provider()
    if not isinstance(rt, dict) or not rt.get("api_key"):
        raise RuntimeError("no Hermes runtime credentials (hermes portal login?)")
    return AIAgent(
        api_key=rt["api_key"],
        base_url=rt.get("base_url") or "",
        provider=rt.get("provider") or "",
        model=model,
        enabled_toolsets=[],          # pure completion path — no tools
        quiet_mode=True,
        platform="cli",
        skip_memory=True,
        skip_context_files=True,
    )


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    model = "stealth/ox-alpha"
    for arg in sys.argv[1:]:
        if arg.startswith("--model="):
            model = arg.split("=", 1)[1]

    agent = None
    build_error = None
    try:
        agent = _build_agent(model)
    except BaseException as exc:  # reported on first request, not fatal:
        build_error = f"{type(exc).__name__}: {exc}"  # parent may retry later

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _emit({"ok": False, "error": "bad json frame"})
            continue

        op = req.get("op")
        if op == "shutdown":
            break
        if op == "ping":
            _emit({"ok": True, "pong": True,
                   "agent_ready": agent is not None,
                   "build_error": build_error})
            continue
        if op != "complete":
            _emit({"id": req.get("id"), "ok": False,
                   "error": f"unknown op: {op!r}"})
            continue

        rid = req.get("id")
        if agent is None:
            # One rebuild attempt per request when the initial build failed.
            try:
                agent = _build_agent(model)
                build_error = None
            except BaseException as exc:
                build_error = f"{type(exc).__name__}: {exc}"
                _emit({"id": rid, "ok": False,
                       "error": f"agent build failed: {build_error}"})
                continue

        t0 = time.monotonic()
        try:
            result = agent.run_conversation(
                req.get("prompt", ""),
                conversation_history=req.get("history") or [],
            )
            content = (result.get("final_response") or "").strip()
            err = result.get("error")
            if not content and err:
                _emit({"id": rid, "ok": False,
                       "error": f"turn failed: {err}"})
            else:
                _emit({"id": rid, "ok": True, "content": content,
                       "elapsed_s": round(time.monotonic() - t0, 2)})
        except BaseException as exc:
            _emit({"id": rid, "ok": False,
                   "error": f"{type(exc).__name__}: {exc}",
                   "agent_dead": True})

    if agent is not None:
        try:
            agent.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
