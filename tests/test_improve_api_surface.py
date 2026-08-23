"""Tests for the API-serving-surface improvement pass (2026-08-23).

Two measured defects, both on the read side of api.py (the write gate and
/admin/sql validator were already covered by test_api_auth.py):

1. UNBOUNDED LIMIT — 12 GET endpoints took a user-supplied ``limit`` straight
   into ``SQL LIMIT ?`` with no server-side cap. SQLite treats LIMIT -1 as
   unlimited, so ``?limit=-1`` (or any huge value) materialises the whole
   table per request. /world/{domain} got exactly this clamp in the
   2026-04-21 audit; these twelve never did.

2. UNGATED SENSITIVE READS — the default-secure middleware only gates WRITE
   methods; reads are protected solely by per-endpoint dependencies. Fourteen
   money/gate/internal GETs (bets history incl. stakes/PnL, hypothesis pool,
   backtest results, research-loop status, order book) had NO dependency, so
   the moment CALLISTO_BIND_HOST is ever set non-loopback they serve real
   position data to unauthenticated callers. Same auth posture as
   GET /task/{id} ("leaks query text") now applied consistently.

Structural tests parse api.py's AST so a future endpoint that reintroduces
either shape FAILS HERE, not in production.
"""

from __future__ import annotations

import ast
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

API_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api.py"
)


def _api_source() -> str:
    with open(API_PATH, encoding="utf-8") as fh:
        return fh.read()


def _get_blocks(src: str):
    """(path, decorator_tail, func_name, signature) for every @app.get."""
    return re.findall(
        r'@app\.get\("([^"]+)"(.*?)\)\nasync def ([^(]+)\((.*?)\)\n',
        src,
        re.S,
    )


# ---------------------------------------------------------------------------
# _cap_limit unit behaviour (imported without triggering the app)
# ---------------------------------------------------------------------------


class TestCapLimit:
    def _helper(self):
        src = _api_source()
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_cap_limit":
                namespace: dict = {}
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<x>", "exec"), namespace)
                return namespace["_cap_limit"]
        pytest.fail("_cap_limit helper missing from api.py")

    def test_negative_becomes_floor_not_unlimited(self):
        # The whole point: SQLite LIMIT -1 == all rows.
        assert self._helper()(-1) == 1

    def test_huge_value_clamped(self):
        assert self._helper()(10**12) == 500

    def test_zero_clamped_to_one(self):
        assert self._helper()(0) == 1

    def test_normal_values_pass_through(self):
        assert self._helper()(25, default=50, cap=500) == 25

    def test_non_numeric_falls_to_default(self):
        assert self._helper()(None) == 50  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Structural pins: no endpoint may reintroduce the defects
# ---------------------------------------------------------------------------


class TestEveryLimitIsCapped:
    def test_every_get_with_a_limit_param_caps_it(self):
        uncapped = []
        for path, _dec, fname, sig in _get_blocks(_api_source()):
            if "limit" not in sig:
                continue
            seg = _api_source()[_api_source().find(f"async def {fname}"):]
            end = seg.find("\n@app")
            seg = seg[:end if end != -1 else 6000]
            if "_cap_limit(" not in seg and "min(int(limit" not in seg:
                uncapped.append(path)
        assert uncapped == [], (
            f"GET endpoints taking `limit` with no server-side cap: {uncapped}. "
            "Use _cap_limit(limit) before the value reaches SQL/`fetchall`."
        )

    def test_known_defect_sites_are_fixed(self):
        src = _api_source()
        for path in [
            "/odds/movements", "/odds/opportunities", "/odds/snapshots/{sport}",
            "/edges/live", "/odds/kl-metrics", "/bets", "/bets/bankroll",
            "/health/integrity/history", "/debug/memory/top-traces",
            "/orders", "/wiki/articles", "/wiki/search",
        ]:
            block = [b for b in _get_blocks(src) if b[0] == path]
            assert block, f"{path} disappeared from api.py?"
            body_start = src.find(f"async def {block[0][2]}")
            body = src[body_start:body_start + 3000]
            assert "_cap_limit(" in body or "min(int(limit" in body, path


class TestSensitiveReadsAreGated:
    SENSITIVE = [
        "/bets", "/bets/bankroll", "/bets/clv-report", "/bets/clv-forecast",
        "/hypothesis", "/hypothesis/{hypothesis_id}",
        "/hypothesis/{hypothesis_id}/report",
        "/hypothesis/{hypothesis_id}/significance",
        "/backtest/run/{run_id}",
        "/research/status", "/research/sports",
        "/claude/status", "/embeddings/stats", "/historical/cache",
    ]

    def test_money_gate_internal_reads_require_loopback_or_admin(self):
        ungated = []
        for path, dec, _f, sig in _get_blocks(_api_source()):
            gated = "require_admin" in sig or "require_admin" in dec
            if path in self.SENSITIVE and not gated:
                ungated.append(path)
        assert ungated == [], (
            f"sensitive GETs without require_admin gating: {ungated}"
        )

    def test_no_new_sensitive_read_regression(self):
        """Any GET whose path touches money/gate/internal vocabulary must be
        explicitly gated — catches future endpoints automatically."""
        vocab = ("/bets", "/orders", "/hypothesis", "/backtest", "/research/",
                 "/executor/", "/admin")
        ungated = [
            p for p, d, _f, s in _get_blocks(_api_source())
            if p.startswith(vocab)
            and "require_admin" not in s and "require_admin" not in d
        ]
        assert ungated == [], ungated
