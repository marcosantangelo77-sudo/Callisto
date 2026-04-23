"""Callisto MCP Server.

Exposes the live Callisto HTTP API (http://localhost:8420 by default) as a set
of native MCP tools over stdio transport. Any MCP-compatible client (Claude
Code, Claude Desktop, the forked CC with Ollama) can load this server and call
Callisto endpoints as tools.

Env vars
--------
- CALLISTO_API_URL: base URL for the Callisto API. Default: http://localhost:8420

CLI
---
- `python tools/callisto_mcp_server.py`               -> run over stdio
- `python tools/callisto_mcp_server.py --list-tools`  -> print tool names and exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


CALLISTO_API_URL = os.environ.get("CALLISTO_API_URL", "http://localhost:8420").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("CALLISTO_MCP_TIMEOUT", "30"))

VALID_DOMAINS = {"FINANCIAL", "TECHNICAL", "SIGNAL", "SYNTHESIS", "GENERAL"}


# ---------------------------------------------------------------------------
# Tool definitions (name -> (description, input JSON schema))
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="callisto_submit_research",
        description=(
            "Submit a research query to Callisto's AGP (Agentic Graph Pipeline) "
            "research loop. Returns the created task_id. Use this for any "
            "investigation/analysis that would benefit from Callisto's evidence "
            "standards, confidence tiers, contradiction checks, and crypto sealing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Research question or directive."},
                "priority": {
                    "type": "integer",
                    "description": "Priority (1 = highest). Default 1.",
                    "default": 1,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="callisto_system_status",
        description=(
            "Return Callisto's full system status: hypotheses, research loop, "
            "embeddings, data collectors, subsystem health, etc. Verbatim JSON "
            "from GET /system/full-status."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="callisto_recent_tasks",
        description="Return the N most recent research tasks submitted to Callisto.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max tasks to return. Default 10.",
                    "default": 10,
                },
            },
        },
    ),
    Tool(
        name="callisto_get_task",
        description="Fetch a specific Callisto task by its task_id, including status and result.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task identifier (integer or string)."},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="callisto_odds_edges",
        description="Current cross-book odds edges detected by Callisto.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="callisto_odds_opportunities",
        description="Current +EV betting opportunities surfaced by Callisto.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="callisto_query_domain",
        description=(
            "Query Callisto's world-memory for a domain. Domain must be one of: "
            "FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, GENERAL."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": sorted(VALID_DOMAINS),
                    "description": "Memory domain to query.",
                },
            },
            "required": ["domain"],
        },
    ),
    Tool(
        name="callisto_sync_context",
        description=(
            "Push session context (summary + actionable queries) to Callisto so "
            "the autonomous system has visibility into the conversation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_summary": {
                    "type": "string",
                    "description": "Short prose summary of the session's insights/decisions.",
                },
                "actionable_queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of follow-up research queries worth submitting.",
                },
            },
            "required": ["session_summary"],
        },
    ),
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _format_result(payload: Any) -> str:
    """Return payload as a pretty-printed JSON string (verbatim JSON where possible)."""
    if isinstance(payload, (dict, list)):
        try:
            return json.dumps(payload, indent=2, default=str)
        except (TypeError, ValueError):
            return str(payload)
    return str(payload)


def _error_payload(where: str, exc: Exception) -> str:
    return json.dumps(
        {
            "error": True,
            "where": where,
            "type": type(exc).__name__,
            "message": str(exc),
            "callisto_api_url": CALLISTO_API_URL,
        },
        indent=2,
    )


async def _get(path: str, params: dict[str, Any] | None = None) -> str:
    url = f"{CALLISTO_API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            try:
                return _format_result(resp.json())
            except ValueError:
                return resp.text
    except Exception as exc:  # noqa: BLE001 — we intentionally surface any failure
        return _error_payload(f"GET {url}", exc)


async def _post(path: str, body: dict[str, Any] | None = None) -> str:
    url = f"{CALLISTO_API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body or {})
            resp.raise_for_status()
            try:
                return _format_result(resp.json())
            except ValueError:
                return resp.text
    except Exception as exc:  # noqa: BLE001
        return _error_payload(f"POST {url}", exc)


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


async def _dispatch(name: str, arguments: dict[str, Any]) -> str:
    arguments = arguments or {}

    if name == "callisto_submit_research":
        query = arguments.get("query")
        if not query:
            return _error_payload("callisto_submit_research", ValueError("'query' is required"))
        priority = int(arguments.get("priority", 1))
        return await _post("/task", {"query": query, "priority": priority})

    if name == "callisto_system_status":
        return await _get("/system/full-status")

    if name == "callisto_recent_tasks":
        limit = int(arguments.get("limit", 10))
        return await _get("/tasks", params={"limit": limit})

    if name == "callisto_get_task":
        task_id = arguments.get("task_id")
        if task_id is None or task_id == "":
            return _error_payload("callisto_get_task", ValueError("'task_id' is required"))
        return await _get(f"/task/{task_id}")

    if name == "callisto_odds_edges":
        return await _get("/odds/edges")

    if name == "callisto_odds_opportunities":
        return await _get("/odds/opportunities")

    if name == "callisto_query_domain":
        domain = str(arguments.get("domain", "")).upper()
        if domain not in VALID_DOMAINS:
            return _error_payload(
                "callisto_query_domain",
                ValueError(f"domain must be one of {sorted(VALID_DOMAINS)}; got {domain!r}"),
            )
        return await _get(f"/world/{domain}")

    if name == "callisto_sync_context":
        summary = arguments.get("session_summary")
        if not summary:
            return _error_payload(
                "callisto_sync_context", ValueError("'session_summary' is required")
            )
        actionable = arguments.get("actionable_queries") or []
        if not isinstance(actionable, list):
            actionable = [str(actionable)]
        return await _post(
            "/context/sync",
            {"session_summary": summary, "actionable_queries": actionable},
        )

    return _error_payload(name, ValueError(f"unknown tool: {name}"))


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------


def _build_server() -> Server:
    server: Server = Server(
        name="callisto",
        version="0.1.0",
        instructions=(
            "Tools for Callisto, an autonomous multi-agent research/betting system "
            "running locally on http://localhost:8420. Use callisto_submit_research "
            "to route investigations through the AGP pipeline; use callisto_system_status "
            "to inspect runtime state before taking action."
        ),
    )

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        text = await _dispatch(name, arguments)
        return [TextContent(type="text", text=text)]

    return server


async def _run_stdio() -> None:
    server = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _main() -> int:
    parser = argparse.ArgumentParser(description="Callisto MCP server (stdio transport).")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print registered MCP tool names (one per line) and exit.",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print tool names + descriptions as JSON and exit.",
    )
    args = parser.parse_args()

    if args.list_tools:
        for tool in TOOLS:
            print(tool.name)
        return 0

    if args.describe:
        print(
            json.dumps(
                [
                    {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                    for t in TOOLS
                ],
                indent=2,
            )
        )
        return 0

    asyncio.run(_run_stdio())
    return 0


if __name__ == "__main__":
    sys.exit(_main())
