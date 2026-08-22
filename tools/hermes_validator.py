"""
Vendored Hermes function-call validation — the only live descendant of the
hermes-function-calling submodule (pinned ea3c4723, four months stale).

VERDICT (instance 5, tier-5 audit, 2026-08-22): ROADMAP's "vendor ~200 lines"
recommendation is RIGHT in spirit but overstated. The genuinely live surface
is ~120 lines:

  - validate_function_call_schema: jsonschema-backed (the upstream hand-rolled
    type checker at validator.py:22 skips falsy args (`if call_arg_value:`)
    and passes bools as ints — strictly weaker than jsonschema; swapped here).
  - The XML + ast.literal_eval extraction ladder from utils.py
    (validate_and_extract_tool_calls): small local models emit Python-literal
    tool calls (single quotes, True/False) that json.loads rejects; the
    literal_eval rescue recovers them.

Everything else in the submodule — chat templates, prompter, torch inference
loop (functioncall.py), demo functions including an exec() liability
(functions.py:38), notebooks — has zero importers in Callisto (verified by
grep; UPSTREAM.md's import claims are false). Native tool calling now lives in
the serving layer (llama-server --jinja, Ollama native tool_calls) and in the
models. Quarantine, don't delete: the submodule stays pinned in git; this
file replaces its use.

Restore note: `git checkout ea3c4723 -- hermes-function-calling` brings back
the full upstream tree.
"""

import ast
import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

import jsonschema


class FunctionCallValidationError(ValueError):
    """Raised when a model tool call fails schema validation."""


def validate_function_call_schema(
    call: dict, signatures: list[dict], raise_on_error: bool = False
) -> tuple[bool, Optional[str]]:
    """Validate a model tool call against OpenAI-style function signatures.

    Args:
        call: {"name": str, "arguments": dict} as produced by the model.
        signatures: [{"type": "function", "function": {"name", "parameters": <json schema>}}]

    Returns (ok, error_message). With raise_on_error=True, raises
    FunctionCallValidationError instead — use at money-adjacent call sites
    where a silently-accepted malformed call is worse than a loud one.
    """
    name = call.get("name")
    arguments = call.get("arguments")
    if not isinstance(name, str) or not name:
        msg = "tool call missing non-empty 'name'"
        return (False, msg) if not raise_on_error else _raise(msg)
    if not isinstance(arguments, dict):
        msg = f"tool call 'arguments' must be a dict, got {type(arguments).__name__}"
        return (False, msg) if not raise_on_error else _raise(msg)

    for sig in signatures:
        fn = sig.get("function") or {}
        if fn.get("name") != name:
            continue
        schema = fn.get("parameters") or {}
        try:
            jsonschema.validate(instance=arguments, schema=schema)
            return True, None
        except jsonschema.ValidationError as e:
            msg = f"arguments for {name!r} failed schema: {e.message}"
            return (False, msg) if not raise_on_error else _raise(msg)

    msg = f"No matching function signature found for function: {name}"
    return (False, msg) if not raise_on_error else _raise(msg)


def _raise(msg: str):
    raise FunctionCallValidationError(msg)


def extract_tool_calls_xml(assistant_content: str) -> list[dict]:
    """Extract tool calls from <tool_call>...</tool_call> XML blocks.

    The extraction ladder: json.loads first, ast.literal_eval rescue second.
    Small local models routinely emit Python-literal arguments (single-quoted
    keys, True/False) that strict JSON parsing rejects; literal_eval recovers
    them without executing code. This is the one piece of upstream utils.py
    worth carrying forward verbatim in spirit.
    """
    calls: list[dict] = []
    try:
        root = ET.fromstring(f"<root>{assistant_content}</root>")
    except ET.ParseError:
        return calls
    for element in root.findall(".//tool_call"):
        json_text = (element.text or "").strip()
        data = None
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            try:
                data = ast.literal_eval(json_text)
            except (SyntaxError, ValueError):
                continue
        if isinstance(data, dict):
            calls.append(data)
    return calls


# Regex variant for content that isn't well-formed XML around the blocks
# (mirrors inference._extract_hermes_tool_calls, plus the literal_eval rescue).
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def extract_tool_calls_regex(assistant_content: str) -> list[dict]:
    calls: list[dict] = []
    for match in _TOOL_CALL_RE.finditer(assistant_content):
        json_text = match.group(1)
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            try:
                data = ast.literal_eval(json_text)
            except (SyntaxError, ValueError):
                continue
        if isinstance(data, dict):
            calls.append(data)
    return calls
