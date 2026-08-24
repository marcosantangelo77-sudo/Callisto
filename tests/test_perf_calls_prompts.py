"""PERF — prompt SHRINK pins (tests/test_perf_calls_prompts.py).

The measurement brief's finding: leaf answer calls sent up to 4,000 chars
of raw API-JSON PER EVIDENCE ITEM for an output of ~100 chars of JSON.
tools.pipeline.model.render_evidence now budgets the payload:

  - each item clipped to PER_ITEM_CAP, with an explicit marker
  - whole payload under TOTAL_EVIDENCE_BUDGET
  - item ORDER preserved; one line per item ALWAYS (a hidden item leaves a
    "not shown" marker so the model knows its view is partial)
  - empty evidence renders "(none)" exactly as before
"""
from __future__ import annotations

from tools.pipeline.model import (
    ANSWER_SYSTEM,
    PER_ITEM_CAP,
    TOTAL_EVIDENCE_BUDGET,
    answer_messages,
    render_evidence,
)


def _big(n: int, ch: str = "x") -> str:
    return ch * n


def test_small_evidence_unchanged():
    ev = ["short one", "short two"]
    out = render_evidence(ev)
    assert "- [0] short one" in out and "- [1] short two" in out


def test_empty_evidence_renders_none():
    assert render_evidence([]) == "(none)"
    msgs = answer_messages("q?", [])
    assert "(none)" in msgs[1]["content"]


def test_per_item_cap_applies_with_marker():
    out = render_evidence([_big(PER_ITEM_CAP + 500)])
    line = out.split("\n")[0]
    assert len(line) < PER_ITEM_CAP + 80          # actually clipped
    assert "truncated" in line                     # honestly marked
    assert line.startswith("- [0] ")


def test_total_budget_holds_for_many_items():
    items = [_big(1500, ch=c) for c in "abcdefg"]
    out = render_evidence(items)
    body_chars = sum(len(ln) for ln in out.split("\n"))
    assert body_chars <= TOTAL_EVIDENCE_BUDGET + 8 * 60  # markers included
    # every item still has a line — hidden ones say so
    for i in range(len(items)):
        assert f"- [{i}]" in out


def test_order_is_preserved_under_budget_pressure():
    items = [_big(2000, "a"), _big(30, "b"), _big(2000, "c")]
    out = render_evidence(items)
    lines = out.split("\n")
    assert lines[0].startswith("- [0] aa")
    assert lines[1].startswith("- [1] bb")
    idx_b = out.index("bb")
    idx_c = out.index("ccc") if "ccc" in out else out.rindex("cc")
    assert idx_b > 0


def test_answer_messages_prompt_size_is_bounded():
    """The regression this exists for: three max-size fetch bodies must not
    produce a 12k+ char prompt anymore."""
    msgs = answer_messages("Will X beat consensus?",
                           [_big(4000), _big(4000), _big(4000)])
    user = msgs[1]["content"]
    assert len(msgs[0]["content"]) == len(ANSWER_SYSTEM)
    assert len(user) < TOTAL_EVIDENCE_BUDGET + 400   # question + markers
    assert "QUESTION: Will X beat consensus?" in user


def test_budget_constants_are_tight_but_sane():
    """Guard against well-meaning drift: big enough to carry substance,
    small enough to actually be a shrink."""
    assert 200 <= PER_ITEM_CAP <= 2500
    assert 500 <= TOTAL_EVIDENCE_BUDGET <= 8000
    assert TOTAL_EVIDENCE_BUDGET >= PER_ITEM_CAP
