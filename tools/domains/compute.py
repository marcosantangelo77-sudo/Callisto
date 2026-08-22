"""Compute plugin — B2's sandbox (tools/sandbox.py, branch
build/sandbox-artifacts) exposed as a domain-general research tool.

Registered with always=True: any session, any domain, may run sealed
Python. The sandbox denies network, scrubs env, rlimits, and destroys its
workspace — the code and its output are sealable evidence
(BUILD_MANDATE item 2, property 3: verifiable, not voluminous).

Guarded import: until B2's branch merges, the plugin is simply not
registered and the registry degrades to core tools — never a hard failure.
"""

import asyncio
import logging

from tools.domain_registry import DomainPlugin

logger = logging.getLogger("callisto.compute_plugin")

RUN_PYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": (
            "Run Python code in a sandbox (no network, no env secrets, "
            "resource-limited). Assign a JSON-serialisable value to "
            "`result` for structured output; print() for logs; files "
            "written to the cwd are captured. Use for real computation: "
            "models, math over evidence, charts data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute"},
                "inputs": {
                    "type": "object",
                    "description": "Optional JSON data delivered as <name>.json files",
                },
            },
            "required": ["code"],
        },
    },
}


def _run_sandbox(name: str, arguments: dict) -> dict:
    from tools.sandbox import run_python
    res = run_python(
        arguments.get("code", ""),
        inputs=arguments.get("inputs") or None,
    )
    return res.to_dict() if hasattr(res, "to_dict") else dict(vars(res))


def build_compute_plugin() -> DomainPlugin:
    loop = asyncio.get_event_loop()

    async def execute(name: str, arguments: dict):
        return await loop.run_in_executor(None, _run_sandbox, name, arguments)

    return DomainPlugin(
        name="compute",
        domains=set(),
        always=True,  # domain-general: every session may compute
        tool_schemas=[RUN_PYTHON_TOOL],
        execute=execute,
    )


def register_if_available(registry) -> bool:
    """Register the compute plugin iff tools/sandbox.py imports cleanly."""
    try:
        import tools.sandbox  # noqa: F401
    except ImportError:
        logger.info("compute plugin unavailable (tools/sandbox.py not merged yet)")
        return False
    if "compute" not in {p.name for p in registry.plugins()}:
        registry.register(build_compute_plugin())
    return True
