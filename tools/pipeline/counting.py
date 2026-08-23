"""Call-count and token instrumentation for the pipeline model seam.

PERF mandate step 1: you cannot cut what you have not counted.
CountingModel wraps any PipelineModel/backend, records one entry per call
(role, task_class, prompt chars in, response chars out — chars are a
deterministic proxy for tokens at ~4 chars/token), and renders a table.

It is a pure wrapper: tests inject it around ScriptedModel; production
wraps HermesCliModel / RouterModel without touching them.
"""
from __future__ import annotations

from typing import Optional


class CountingModel:
    """Records every complete() that passes through. Wraps anything with
    an async complete(first_arg, messages, **kw) -> dict."""

    def __init__(self, inner):
        self.inner = inner
        self.records: list[dict] = []

    @property
    def name(self) -> str:
        return getattr(self.inner, "name", "counted")

    @property
    def calls_made(self) -> int:
        return len(self.records)

    def total_chars_in(self) -> int:
        return sum(r["chars_in"] for r in self.records)

    def total_chars_out(self) -> int:
        return sum(r["chars_out"] for r in self.records)

    async def complete(self, *args, **kwargs) -> dict:
        role_or_task = str(args[0]) if args else ""
        messages = args[1] if len(args) > 1 else (kwargs.get("messages") or [])
        schema = kwargs.get("schema")
        chars_in = sum(len(str(m.get("content", ""))) for m in messages)
        resp = await self.inner.complete(*args, **kwargs)
        content = resp.get("content") if isinstance(resp, dict) else None
        if content is None and isinstance(resp, dict):
            import json as _json
            content = _json.dumps(resp.get("parsed_json", ""))
        self.records.append({
            "role": role_or_task,
            "schema": bool(schema),
            "chars_in": chars_in,
            "chars_out": len(content or ""),
            "cache": resp.get("cache", "miss"),
        })
        return resp

    def table(self) -> str:
        rows = ["idx  role/task      cache  in_ch  out_ch",
                "---  -------------  -----  -----  -----"]
        for i, r in enumerate(self.records):
            rows.append(f"{i:<3}  {r['role'][:13]:<13}  "
                        f"{r['cache']:<5}  {r['chars_in']:>5}  "
                        f"{r['chars_out']:>5}")
        n = len(self.records)
        rows.append(f"calls: {n}   chars in: {self.total_chars_in()}   "
                    f"chars out: {self.total_chars_out()}")
        return "\n".join(rows)


def estimate_tokens(chars: int) -> int:
    """~4 chars per token for English prose + JSON. A proxy for reporting,
    never used in control flow."""
    return max(1, chars // 4)
