"""
Smoke test: exercise the main Claude-calling paths in CALLISTO_LOCAL_ONLY=1
and print PASS/FAIL. Exits 0 on clean, 1 on any leak.

Usage: python scripts/verify_local_only.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

# Force the kill switch ON before any Callisto import.
os.environ["CALLISTO_LOCAL_ONLY"] = "1"
# Make sure the project root is importable when run as a script.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ OK ] {label}")
    else:
        FAILURES.append(label)
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    print("=== CALLISTO_LOCAL_ONLY smoke test ===")
    print(f"env CALLISTO_LOCAL_ONLY = {os.environ.get('CALLISTO_LOCAL_ONLY')!r}")

    # 1. Helper
    try:
        from tools.local_only import is_local_only, local_only_result
        check("is_local_only() == True", is_local_only() is True)
        r = local_only_result()
        check("local_only_result.error == blocked_by_local_only",
              r.get("error") == "blocked_by_local_only")
        check("local_only_result.local_only == True", r.get("local_only") is True)
    except Exception as e:
        check("tools.local_only import", False, f"{type(e).__name__}: {e}")
        return 1

    # 2. claude_code module
    try:
        import tools.claude_code as cc
        check("claude_code.is_available() == False", cc.is_available() is False)
        qr = await cc.claude_code_query("hello")
        check("claude_code_query blocked",
              qr.get("error") == "blocked_by_local_only",
              f"got {qr!r}")
        sr = cc.claude_code_sync("hello")
        check("claude_code_sync blocked",
              sr.get("error") == "blocked_by_local_only")
    except Exception as e:
        traceback.print_exc()
        check("tools.claude_code paths", False, str(e))

    # 3. Ladder: with claude_code rungs stripped, ladder should route to
    # local models. We patch in a fake agent to avoid needing Ollama.
    try:
        import inference
        from unittest.mock import patch

        class FakeAgent:
            async def achat(self, messages, options=None, **kw):
                return {"content": '[{"name":"n","market":"m","edge_logic":"l","min_signals":1}]',
                        "tool_calls": [], "parsed_json": None}

        async def boom_claude(*a, **kw):
            raise AssertionError("claude_code_query must not be called")

        with patch.object(inference, "_get_inference", lambda m: FakeAgent()), \
             patch("tools.claude_code.claude_code_query", boom_claude), \
             patch("tools.local_cc_bridge.should_use_bridge", lambda t: False):
            res = await inference.escalate_with_ladder(
                "smoke", task_type="reasoning"
            )
            check("escalate_with_ladder returned content",
                  bool(res.get("content")))
            check("escalate_with_ladder did NOT use claude_code",
                  res.get("model_used") != "claude_code",
                  f"model_used={res.get('model_used')}")
    except Exception as e:
        traceback.print_exc()
        check("escalate_with_ladder smoke", False, str(e))

    # 4. Bridge gating
    try:
        import tools.local_cc_bridge as bridge
        # No LOCAL_CC_PATH set + no autodetect → should be False even if
        # local_only is True, because the binary doesn't exist.
        # But if autodetect happens to find something on the dev box,
        # this may return True — in that case we just assert it's a
        # bool (the gating function isn't raising).
        assert isinstance(bridge.should_use_bridge("reasoning"), bool)
        check("local_cc_bridge.should_use_bridge returns bool", True)
    except Exception as e:
        traceback.print_exc()
        check("local_cc_bridge smoke", False, str(e))

    print()
    if FAILURES:
        print(f"=== FAIL ({len(FAILURES)} check(s) failed) ===")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("=== PASS ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
