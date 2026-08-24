"""REVIEW — B1 half-landed: the trusted-formula write path takes spec bytes.

Family 2: a fix lands in one copy while another keeps the bug.

fix_b1.md guarded every fetched-bytes cell with _guarded_text() and declared
the ONLY unguarded writes are Model column C / ModelLive, "fed exclusively
from spec['model'][*]['formula'], produced by our code paths (finance plugin,
fermi), never from fetched bytes."

That provenance claim is enforced by NOTHING. build_workbook(spec) is a
public function taking an arbitrary dict; any caller — a future plugin, a
restored artifact spec, an LLM tool-call argument — can put attacker bytes in
spec["model"][*]["formula"] and get a live formula executed by Excel on open.
The guard exists; the trust boundary is a comment.

Repro FAILS on current master: build_workbook happily emits data_type 'f'.
"""
import io

import pytest

openpyxl = pytest.importorskip("openpyxl")

from tools.charts import build_workbook


def test_model_formula_path_executes_spec_bytes():
    """A string that arrived from outside the process, placed in the one
    unguarded slot, becomes a live Excel formula."""
    payload = 'HYPERLINK("http://evil","click")'
    wb = build_workbook({
        "assumptions": [], "data": {},
        "model": [{"cell": "B2", "label": "x", "formula": payload}],
    })
    book = openpyxl.load_workbook(io.BytesIO(wb))
    cell = book["ModelLive"]["B2"]
    assert not (cell.data_type == "f" and "evil" in str(cell.value)), (
        "attacker-controlled spec bytes written as live formula "
        "(B1 fix trusts spec['model'] provenance that nothing enforces)")
