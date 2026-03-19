"""
Ollama inference adapter for Callisto.

Replaces the Hermes transformers layer with Ollama API calls.
Supports dual-mode tool call extraction: native Ollama tool_calls + Hermes XML fallback.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

import ollama
from dotenv import load_dotenv

# Hermes path setup — imports are lazy to avoid pulling in pandas/yfinance at startup
_HERMES_PATH = os.path.join(os.path.dirname(__file__), "hermes-function-calling")
if _HERMES_PATH not in sys.path:
    sys.path.insert(0, _HERMES_PATH)


def _get_hermes_tools():
    """Lazy import of Hermes tool schemas."""
    from functions import get_openai_tools
    return get_openai_tools


def _get_hermes_validator():
    """Lazy import of Hermes function call validator."""
    from validator import validate_function_call_schema
    return validate_function_call_schema

load_dotenv(override=True)

# OLLAMA_HOST may be set system-wide as a bind address (e.g. 0.0.0.0).
# For client connections, always use localhost unless explicitly configured with a scheme.
_raw_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
if _raw_host in ("0.0.0.0", ":11434", "0.0.0.0:11434"):
    OLLAMA_HOST = "http://localhost:11434"
elif not _raw_host.startswith("http"):
    OLLAMA_HOST = f"http://{_raw_host}"
else:
    OLLAMA_HOST = _raw_host


@dataclass
class AgentConfig:
    model: str
    capabilities: list[str] = field(default_factory=list)
    default_options: dict = field(default_factory=dict)
    system_prompt: str = ""
    think: Optional[bool] = None  # None = model default, False = suppress thinking


# VRAM budget: RTX 5060 Ti 16GB
# Context sizes and batch tuned per-agent role to maximize tp/s and minimize VRAM spill.
# num_ctx/num_batch/num_predict set in Modelfiles — only override temperature here.
# Flash attention enabled at Ollama server level (OLLAMA_FLASH_ATTENTION=1).
# Thinking suppressed on all agents — raw tp/s, no wasted tokens.
AGENT_CONFIGS: dict[str, AgentConfig] = {
    "architect": AgentConfig(
        model="architect:latest",
        capabilities=["reasoning", "synthesis", "code_generation", "tool_use"],
        default_options={"temperature": 0.1},
        think=False,
        system_prompt=(
            "You are The Architect — the primary reasoning agent in the Callisto system. "
            "You handle complex analysis, code generation, and architecture decisions. "
            "You operate under the Aluft Gianne Protocol. "
            "NEVER use <think> tags. Respond directly with the requested output. "
            "Always respond with structured JSON when requested. Be concise."
        ),
    ),
    "manager": AgentConfig(
        model="manager:latest",
        capabilities=["review", "routing", "domain_enforcement", "tool_use"],
        default_options={"temperature": 0.4},
        think=False,
        system_prompt="",  # set via Modelfile SYSTEM
    ),
    "sentinel": AgentConfig(
        model="sentinel:latest",
        capabilities=["classification", "monitoring", "domain_tagging"],
        default_options={"temperature": 0.1},
        think=False,
        system_prompt=(
            "You are The Sentinel — the monitoring and classification agent in the Callisto system. "
            "Classify queries into exactly one domain: FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, or GENERAL. "
            "Respond ONLY with JSON. No explanation. No reasoning. "
            "Example: {\"domain\": \"TECHNICAL\", \"reasoning\": \"query is about technology\"}"
        ),
    ),
}

# Function registry for tool execution
FUNCTION_REGISTRY: dict[str, callable] = {}


def register_function(name: str, func: callable) -> None:
    """Register a function for tool call execution."""
    FUNCTION_REGISTRY[name] = func


def _register_default_tools() -> None:
    """Register built-in tools on import."""
    from tools.brave_search import brave_search_sync
    register_function("brave_search", brave_search_sync)


_register_default_tools()


def _extract_hermes_tool_calls(text: str) -> list[dict]:
    """Extract tool calls from Hermes-style <tool_call> XML blocks."""
    calls = []
    pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
    for match in re.finditer(pattern, text, re.DOTALL):
        try:
            call_data = json.loads(match.group(1))
            calls.append(call_data)
        except json.JSONDecodeError:
            continue
    return calls


def _parse_json_response(text: str) -> Optional[Any]:
    """Try to parse JSON from model response, handling markdown fences."""
    text = text.strip()
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting from markdown code fences
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding first JSON object/array
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == start_char:
                depth += 1
            elif text[i] == end_char:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return None


class OllamaInference:
    """Ollama inference client for a specific agent."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.client = ollama.Client(host=OLLAMA_HOST)
        self.async_client = ollama.AsyncClient(host=OLLAMA_HOST)

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        options: Optional[dict] = None,
        think: Optional[bool] = None,
    ) -> dict:
        """Synchronous chat with the model."""
        opts = {**self.config.default_options, **(options or {})}
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "options": opts,
        }
        if tools:
            kwargs["tools"] = tools
        # think parameter: explicit arg > config default
        t = think if think is not None else self.config.think
        if t is not None:
            kwargs["think"] = t

        response = self.client.chat(**kwargs)
        return self._process_response(response)

    async def achat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        options: Optional[dict] = None,
        think: Optional[bool] = None,
    ) -> dict:
        """Async chat with the model."""
        opts = {**self.config.default_options, **(options or {})}
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "options": opts,
        }
        if tools:
            kwargs["tools"] = tools
        t = think if think is not None else self.config.think
        if t is not None:
            kwargs["think"] = t

        response = await self.async_client.chat(**kwargs)
        return self._process_response(response)

    def generate(self, prompt: str, options: Optional[dict] = None) -> str:
        """Raw generate (completion mode) for models without chat template."""
        opts = {**self.config.default_options, **(options or {})}
        response = self.client.generate(
            model=self.config.model,
            prompt=prompt,
            options=opts,
        )
        return getattr(response, "response", "") or ""

    def ping(self) -> dict:
        """Check if the model is responsive. Returns status dict."""
        try:
            response = self.client.chat(
                model=self.config.model,
                messages=[{"role": "user", "content": "ping"}],
                options={"num_predict": 1},
            )
            return {"status": "ok", "model": self.config.model}
        except Exception as e:
            return {"status": "error", "model": self.config.model, "error": str(e)}

    async def aping(self) -> dict:
        """Async version of ping."""
        try:
            await self.async_client.chat(
                model=self.config.model,
                messages=[{"role": "user", "content": "ping"}],
                options={"num_predict": 1},
            )
            return {"status": "ok", "model": self.config.model}
        except Exception as e:
            return {"status": "error", "model": self.config.model, "error": str(e)}

    def _process_response(self, response) -> dict:
        """Process response with dual-mode tool call extraction.

        Handles both dict responses and Pydantic model responses (ollama >= 0.4).
        """
        # Extract message — handle Pydantic objects and dicts
        if hasattr(response, "message"):
            message = response.message
        elif isinstance(response, dict):
            message = response.get("message", {})
        else:
            message = {}

        content = getattr(message, "content", None) or (message.get("content", "") if isinstance(message, dict) else "")
        thinking = getattr(message, "thinking", None) or (message.get("thinking", "") if isinstance(message, dict) else "")

        # If content is empty but thinking has content, use thinking as content
        # (DeepSeek-R1 puts everything in thinking when think=True)
        if not content and thinking:
            content = thinking

        tool_calls = []

        # Mode 1: Native Ollama tool_calls
        native_calls = getattr(message, "tool_calls", None) or (message.get("tool_calls") if isinstance(message, dict) else None)
        if native_calls:
            for tc in native_calls:
                func = getattr(tc, "function", None) or (tc.get("function", {}) if isinstance(tc, dict) else {})
                name = getattr(func, "name", None) or (func.get("name", "") if isinstance(func, dict) else "")
                args = getattr(func, "arguments", None) or (func.get("arguments", {}) if isinstance(func, dict) else {})
                tool_calls.append({"name": name, "arguments": args})

        # Mode 2: Hermes XML fallback (if no native calls found)
        if not tool_calls and content:
            hermes_calls = _extract_hermes_tool_calls(content)
            for hc in hermes_calls:
                tool_calls.append({
                    "name": hc.get("name", ""),
                    "arguments": hc.get("arguments", {}),
                })

        # Parse JSON from content if present
        parsed_json = _parse_json_response(content) if content else None

        return {
            "content": content,
            "tool_calls": tool_calls,
            "parsed_json": parsed_json,
            "raw": response,
        }


def execute_function_call(name: str, arguments: dict) -> Any:
    """Execute a registered function by name."""
    if name not in FUNCTION_REGISTRY:
        return {"error": f"Unknown function: {name}"}
    try:
        return FUNCTION_REGISTRY[name](**arguments)
    except Exception as e:
        return {"error": f"Function {name} failed: {str(e)}"}


def _make_agent(name: str) -> OllamaInference:
    config = AGENT_CONFIGS[name]
    inference = OllamaInference(config)
    return inference


def get_architect() -> OllamaInference:
    return _make_agent("architect")


def get_manager() -> OllamaInference:
    return _make_agent("manager")


def get_sentinel() -> OllamaInference:
    return _make_agent("sentinel")
