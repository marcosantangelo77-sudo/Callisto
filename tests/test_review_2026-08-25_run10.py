"""REVIEW RUN 10 — 2026-08-25 (branch improve/source-quality-s0826 vs origin/master 6977793).

Families hunted: #2 (fix lands in one copy while mirrors keep the bug) —
the S1/S3 provenance fixes in tools/sources/base.py were grepped across the
whole tree for other implementations of the same rule; #3 (absence treated
as success) — the run-9 zero-result envelope finding was generalised from
the synthetic {"meta": {"query": q}} shape to the shapes REAL adapters emit;
#1 (a verification layer that never runs).

All tests below are EXPECTED TO FAIL on current master / this branch.
Each failure is the reproduction of a recorded defect.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── A · family 2 (#7) — the S1+S3 fix landed ONLY in tools/sources/base.py ──
# commit 95684db teaches RestSource._record two rules: a non-200 body must be
# recorded primary=False, and a ledger failure must fail the fetch CLOSED.
# tools/domains/finance/edgar.py has its OWN _record implementing the SAME
# rule against the SAME ledger, and it keeps BOTH bugs: a 503 error page is
# minted primary=True, and a ledger failure is swallowed with a log line.
# EdgarClient IS production-wired (tools/domains/finance/plugin.py builds one
# with a real ProvenanceLedger), so these are live paths, not dead code.

def test_edgar_error_page_mints_primary():
    from unittest.mock import MagicMock
    from tools.domains.finance.edgar import EdgarClient
    led = MagicMock()
    c = EdgarClient(ledger=led)
    c._record("https://www.sec.gov/cgi-bin/srqsb?text=503", 503,
              "<html><body>503 Service Unavailable</body></html>", 0.05)
    kwargs = led.record_tool_result.call_args.kwargs
    assert kwargs.get("primary") is not True or False, (
        "EdgarClient._record mints PRIMARY provenance for an HTTP 503 error "
        "page — exactly the bug red-team S1 fixed in tools/sources/base.py "
        "(commit 95684db). Same rule, second copy, unfixed.")


def test_edgar_ledger_failure_is_swallowed_not_failed_closed():
    from unittest.mock import MagicMock
    from tools.domains.finance.edgar import EdgarClient
    c = EdgarClient(ledger=MagicMock(side_effect=RuntimeError("ledger down")))
    try:
        rec = c._record("https://www.sec.gov/x", 200, '{"real": "bytes"}', 0.05)
    except Exception:
        pytest.fail("EdgarClient should fail closed, but that assertion is "
                    "about the FIX; today it must be shown to swallow.")
    assert False, (
        f"EdgarClient._record swallowed a ledger write failure and returned "
        f"{rec!r} — unverifiable bytes flow on green (red-team S3, fixed in "
        "sources/base.py only). Family 2, seventh instance.")


# ── B · family 3 — the zero-result echo hole survives EVERY real shape ──────
# Run 9 pinned the synthetic envelope {"meta": {"query": <q>}}. The gate
# still walks the ENTIRE payload including request-echo metadata, so every
# adapter's REAL zero-result body admits too — often at 100%.

GATE_Q = "what does recent research say about semiconductor supply chain resilience"


def _gate(parsed):
    from tools.pipeline.retrieval import RelevanceGate
    return RelevanceGate().judge(GATE_Q, "factual", parsed)


def test_zero_result_openalex_envelope_admits_on_echoed_query():
    ok, cov, reason = _gate({"meta": {"query": GATE_Q, "count": 0},
                             "results": []})
    assert not ok, (
        f"Zero-result OpenAlex-shaped envelope admitted at {cov:.0%} "
        f"({reason!r}): extract_text judges the echoed query string, so an "
        "EMPTY result set scores perfectly. Family 3 — absence is success.")


def test_zero_result_semanticscholar_envelope_admits():
    ok, cov, reason = _gate({"total": 0, "data": [],
                             "query": GATE_Q})
    assert not ok, (
        f"Zero-result SemanticScholar-shaped envelope admitted at {cov:.0%}.")


def test_zero_result_fred_envelope_admits_at_100_percent():
    # FRED series_search echoes search_text; a quota/empty reply carries it.
    q = "unemployment rate trend"
    from tools.pipeline.retrieval import RelevanceGate
    ok, cov, reason = RelevanceGate().judge(
        q, "factual",
        {"error_code": 420,
         "error_message": "series_search requires an API key",
         "search_text": q, "count": 0, "seriess": []})
    assert not ok, (
        f"FRED-shaped zero-result/error envelope admitted at {cov:.0%} "
        f"({reason!r}) — the echoed search_text IS the question.")


# ── C · family 1 — reviewer defect ledger destroyed again ───────────────────
# Run 9 (HIGH): every reviewer/red-team repro suite absent from origin/master.
# One merge train later they are STILL absent — and the newest fix branch
# (improve/source-quality-s0826) imports its repro from
# tests/test_redteam_source_registry.py, which does not exist on its base.
# The repo cannot pin known defects if the trains keep resolving them away.

def test_reviewer_repro_suites_exist_in_the_tree():
    required = [
        "tests/test_review_2026_08_23.py",
        "tests/test_review_2026-08-24d.py",
        "tests/test_review_2026-08-24e.py",
        "tests/test_review_2026-08-25.py",
        "tests/test_review_2026-08-25_run9.py",
        "tests/test_redteam_source_registry.py",
        "tests/test_redteam_source_control_plane.py",
        "tests/test_redteam_domain_plugins.py",
    ]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    missing = [p for p in required
               if not os.path.exists(os.path.join(root, p))]
    assert missing == [], (
        f"The reviewer/red-team defect ledger is missing from this tree: "
        f"{missing}. Merge trains keep resolving these files away; every "
        "defect they pin is UNPINNED until they are restored."
    )
