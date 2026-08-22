"""Tier 5 — vendored Hermes validator tests (instance 5).

Run: python3 -m pytest tests/test_tier5_serving_hermes_validator.py -x -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.hermes_validator import (
    FunctionCallValidationError,
    extract_tool_calls_regex,
    extract_tool_calls_xml,
    validate_function_call_schema,
)

SIG = [
    {
        "type": "function",
        "function": {
            "name": "get_odds",
            "description": "Get odds",
            "parameters": {
                "type": "object",
                "properties": {
                    "sport": {"type": "string", "enum": ["nba", "nfl"]},
                    "limit": {"type": "integer", "minimum": 1},
                    "live": {"type": "boolean"},
                },
                "required": ["sport"],
            },
        },
    }
]


class TestValidateFunctionCallSchema:
    def test_valid_call_passes(self):
        ok, err = validate_function_call_schema(
            {"name": "get_odds", "arguments": {"sport": "nba", "limit": 5}}, SIG
        )
        assert ok and err is None

    def test_missing_required_fails(self):
        ok, err = validate_function_call_schema(
            {"name": "get_odds", "arguments": {}}, SIG
        )
        assert not ok and "sport" in err

    def test_unknown_function_fails(self):
        ok, err = validate_function_call_schema(
            {"name": "nope", "arguments": {}}, SIG
        )
        assert not ok and "No matching" in err

    def test_bool_not_accepted_as_int(self):
        # The upstream hand-rolled checker passed bools as ints
        # (isinstance(True, int) is True in Python). jsonschema must not.
        ok, _ = validate_function_call_schema(
            {"name": "get_odds", "arguments": {"sport": "nba", "limit": True}}, SIG
        )
        assert not ok

    def test_enum_enforced(self):
        ok, _ = validate_function_call_schema(
            {"name": "get_odds", "arguments": {"sport": "golf"}}, SIG
        )
        assert not ok

    def test_minimum_enforced(self):
        ok, _ = validate_function_call_schema(
            {"name": "get_odds", "arguments": {"sport": "nba", "limit": 0}}, SIG
        )
        assert not ok

    def test_raise_on_error_mode(self):
        try:
            validate_function_call_schema(
                {"name": "get_odds", "arguments": {}}, SIG, raise_on_error=True
            )
            raised = False
        except FunctionCallValidationError:
            raised = True
        assert raised

    def test_malformed_call_shape(self):
        assert validate_function_call_schema({"arguments": {}}, SIG)[0] is False
        assert validate_function_call_schema({"name": "x", "arguments": []}, SIG)[0] is False


class TestExtractionLadder:
    def test_xml_json_block(self):
        content = '<tool_call>\n{"name": "get_odds", "arguments": {"sport": "nba"}}\n</tool_call>'
        calls = extract_tool_calls_xml(content)
        assert calls == [{"name": "get_odds", "arguments": {"sport": "nba"}}]

    def test_regex_python_literal_rescue(self):
        # Small local models emit single-quoted Python literals; json.loads
        # rejects them, ast.literal_eval recovers them without executing code.
        content = "<tool_call>{'name': 'get_odds', 'arguments': {'sport': 'nba'}}</tool_call>"
        calls = extract_tool_calls_regex(content)
        assert calls[0]["name"] == "get_odds"

    def test_never_executes_code(self):
        # literal_eval, unlike eval, refuses arbitrary expressions.
        content = "<tool_call>__import__('os').system('touch /tmp/pwned')</tool_call>"
        assert extract_tool_calls_regex(content) == []

    def test_garbage_returns_empty(self):
        assert extract_tool_calls_regex("<tool_call>not a dict at all</tool_call>") == []
        assert extract_tool_calls_xml("<root><unclosed>") == []

    def test_multiple_calls(self):
        content = (
            '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
            'some prose'
            '<tool_call>{"name": "b", "arguments": {"x": 1}}</tool_call>'
        )
        names = [c["name"] for c in extract_tool_calls_regex(content)]
        assert names == ["a", "b"]
