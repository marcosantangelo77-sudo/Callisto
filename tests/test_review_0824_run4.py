"""Review 2026-08-24 run 4 — review of review/ox-alpha-0824b landing (db08c13)
and the known-answer-harness / mutation-testing merges.

Every test reproduces a defect found this run and FAILS on master db08c13
for the documented reason (the defect is live). When its fix lands, flip it
to a passing fix-pin.

Families per PATTERNS.md:
  Q1/Q2  family 3 (absence/shape treated as success) in the NEW quant gate
         shipped by 38aee73 — the gate that replaced "any digit" still
         admits non-quantities.
  Q3     families 1+3: cmefedfut turns a 502 error body into an honest-
         looking empty result backed by PRIMARY provenance (run-3 R4,
         re-pinned against master after the merge train).
  Q4     family 1: RestSource.get_json hands a parsed error body to direct
         adapter callers as data while _record already minted PRIMARY
         (run-3 R1/R2, still live on master).
"""
import json

import pytest


# ── Q1: a year followed by a period escapes _YEAR_RE and satisfies the gate ──
def test_q1_year_before_sentence_period_is_not_quantity():
    from tools.pipeline.engine import _prose_carries_quantity

    # The pinned case ("In 2023 the rate was high") strips because the year
    # is followed by a word boundary. A year at the end of a SENTENCE is
    # followed by '.', which [^\w.] deliberately excludes — so the year is
    # NOT stripped and its digit satisfies quant_required:
    assert not _prose_carries_quantity(
        "The rate was considered elevated in 2020."), \
        "a bare year before a full stop counted as quantitative evidence"
    assert not _prose_carries_quantity(
        "Commentators wrote about 2019. Nothing more."), \
        "second sentence-initial variant"


# ── Q1b: glued year tokens (FY2020) bypass the stripper entirely ────────────
def test_q1b_glued_year_token_is_not_quantity():
    from tools.pipeline.engine import _prose_carries_quantity

    assert not _prose_carries_quantity("In FY2020 spending rose."), \
        "'FY2020' — a year glued to letters — counted as a quantity"
    assert not _prose_carries_quantity("Since2020 policy changed.")


# ── Q2: NaN structured return value counts as quantitative production ──────
def test_q2_nan_sandbox_return_value_is_not_quantitative():
    from tools.pipeline.engine import _produced_quantitative

    class _Sbx:
        status = "ok"
        return_value = float("nan")   # survives JSON round-trip as NaN

    assert not _produced_quantitative("", _Sbx()), \
        "sandbox returning NaN satisfied quant_required"


# ── Q3: cmefedfut mints PRIMARY over a 502 and reports zero settlements ────
def test_q3_cmefedfut_502_does_not_look_like_an_empty_day():
    from tools.sources.base import RestSource
    from tools.sources.cmefedfut import CmeFedFutAdapter

    class FakeTransport:
        def __call__(self, url, headers):
            return 502, '{"error": "gateway timeout"}'

    class FakeSpec:
        name = "cmefedfut"
        base_url = "http://x"
        min_interval_s = 0
        tier = 3
        headers = [("User-Agent", "t")]
        key_env_var = ""

        def build_url(self, path="", params=None):
            return "http://x/settlements?tradeDate=20250815"

    class Ledger:
        def __init__(self):
            self.entries = []

        def record_tool_result(self, tool, content, primary=False, urls=None):
            self.entries.append((tool, primary))
            return "obs1"

    led = Ledger()
    rs = RestSource(FakeSpec(), ledger=led, transport=FakeTransport())
    out = CmeFedFutAdapter(rs).settlements("20250815")

    # The adapter must not present a gateway failure as "no contracts listed":
    assert "error" not in json.dumps(out).lower() or out["settlements"], \
        "502 error body became {'settlements': []} — an honest-looking null"
    # ...and its bytes must never have entered the ledger as PRIMARY:
    assert all(not primary for _, primary in led.entries), \
        f"ledger minted PRIMARY for a 502 body: {led.entries}"


# ── Q4: get_json returns an error body as data; trust was assigned in _record ─
def test_q4_get_json_does_not_hand_back_error_bodies_as_data():
    from tools.sources.base import RestSource

    class FakeTransport:
        def __call__(self, url, headers):
            return 503, '{"error": "service unavailable"}'

    class FakeSpec:
        name = "x"
        base_url = "http://x"
        min_interval_s = 0
        tier = 3
        headers = [("User-Agent", "t")]
        key_env_var = ""

        def build_url(self, path="", params=None):
            return "http://x"

    class Ledger:
        def record_tool_result(self, tool, content, primary=False, urls=None):
            assert not primary, \
                "ledger minted PRIMARY for a 503 body inside _record, " \
                "before any consumer judged the status code"
            return "obs1"

    rs = RestSource(FakeSpec(), ledger=Ledger(), transport=FakeTransport())
    with pytest.raises(Exception) as ei:
        rs.get_json("http://x")
    assert "service unavailable" not in str(ei.value), \
        "the raw parsed error body leaked through as data"
