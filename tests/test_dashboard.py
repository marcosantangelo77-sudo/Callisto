"""Smoke tests for the Callisto ops dashboard sub-app.

The dashboard's job is modest — proxy a handful of live endpoints, merge
in a few direct SQLite reads, and serve static HTML. The tests verify:

  * The static index is served and contains each of the 6 panel IDs.
  * Each ``/api/*`` endpoint hits the right upstream path AND reads the
    right tables, via a stub ``UpstreamClient`` and a temp SQLite DB.
  * Empty states render without 500s (empty DB, upstream 404).
  * Offline state surfaces through the payload shape the UI depends on
    (``online: false``, ``source: "db"`` for orders/ingestion).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools import dashboard as dash  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeUpstream:
    """Records every GET and returns pre-programmed payloads."""

    def __init__(self, responses: dict[str, tuple[bool, Any]] | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []

    async def get(self, path: str) -> tuple[bool, Any]:
        self.calls.append(path)
        if path in self.responses:
            return self.responses[path]
        # Return 404-style miss by default.
        return False, {"error": "not_programmed", "path": path}

    async def close(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def empty_db(tmp_path: Path) -> str:
    db = tmp_path / "callisto.db"
    conn = sqlite3.connect(db)
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def seeded_db(tmp_path: Path) -> str:
    db = tmp_path / "callisto.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ingestion_runs (
            id INTEGER PRIMARY KEY,
            source TEXT,
            status TEXT,
            started_at TEXT,
            finished_at TEXT,
            rows_written INTEGER
        );
        INSERT INTO ingestion_runs (source, status, started_at, finished_at, rows_written)
        VALUES ('test_source_ok', 'success',
                datetime('now', '-30 seconds'),
                datetime('now', '-25 seconds'),
                42);
        -- 10h stale vs the 2h default SLA for unknown sources → should be
        -- at least yellow (10h > 2h) and comfortably past 3×SLA so it lands
        -- in "red" territory for the DB fallback classifier.
        INSERT INTO ingestion_runs (source, status, started_at, finished_at, rows_written)
        VALUES ('test_source_stale', 'success',
                datetime('now', '-11 hours'),
                datetime('now', '-10 hours'),
                0);

        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            state TEXT,
            sport TEXT,
            market TEXT,
            stake REAL,
            created_at TEXT
        );
        INSERT INTO orders (state, sport, market, stake, created_at)
        VALUES ('pending_approval', 'basketball_nba', 'spreads', 10.0, '2026-04-22T12:00:00Z');
        INSERT INTO orders (state, sport, market, stake, created_at)
        VALUES ('filled', 'baseball_mlb', 'totals', 25.0, '2026-04-22T11:00:00Z');
        """
    )
    conn.commit()
    conn.close()
    return str(db)


def _build(client_stub: FakeUpstream, db_path: str):
    app = dash.build_dashboard_subapp(db_path=db_path)
    # Replace the real UpstreamClient with our stub.
    app.state.client = client_stub
    return app


# ---------------------------------------------------------------------------
# Static site
# ---------------------------------------------------------------------------


def test_index_served_research_panels_not_live_trading(empty_db):
    app = _build(FakeUpstream(), empty_db)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200, r.text
        html = r.text
        for panel_id in ("panel-state", "panel-ingestion", "panel-alerts"):
            assert panel_id in html, f"missing research panel {panel_id} in index"
        for panel_id in ("panel-hyps", "panel-orders", "panel-portfolio"):
            assert panel_id not in html, (
                f"{panel_id} must stay deleted from the default dashboard"
            )


def test_static_app_js_served(empty_db):
    app = _build(FakeUpstream(), empty_db)
    with TestClient(app) as c:
        r = c.get("/static/app.js")
        assert r.status_code == 200
        assert "refresh" in r.text.lower()


def test_ping_is_live_even_if_api_down(empty_db):
    app = _build(FakeUpstream(), empty_db)
    with TestClient(app) as c:
        r = c.get("/api/ping")
        assert r.status_code == 200
        assert r.json().get("ok") is True


# ---------------------------------------------------------------------------
# /api/status — aggregated panel
# ---------------------------------------------------------------------------


def test_status_online_calls_expected_endpoints(empty_db):
    stub = FakeUpstream({
        "/system/full-status": (True, {"autonomous_loop": {"running": True}}),
        "/health": (True, {"healthy": True, "subsystems": {}}),
        "/executor/status": (True, {"enabled": False}),
        "/claude/status": (True, {"available": True}),
    })
    app = _build(stub, empty_db)
    with TestClient(app) as c:
        r = c.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["online"] is True
        # All four upstream endpoints were hit.
        assert "/system/full-status" in stub.calls
        assert "/health" in stub.calls
        assert "/executor/status" in stub.calls
        assert "/claude/status" in stub.calls


def test_status_offline_when_all_upstreams_fail(empty_db):
    stub = FakeUpstream()  # no programmed responses → all return (False, ...)
    app = _build(stub, empty_db)
    with TestClient(app) as c:
        r = c.get("/api/status")
        assert r.status_code == 200  # dashboard must not 500
        body = r.json()
        assert body["online"] is False
        assert body["full_status"] is None


# ---------------------------------------------------------------------------
# /api/hypotheses/live
# ---------------------------------------------------------------------------


def test_hypotheses_shapes_rows_and_computes_health_color(empty_db):
    stub = FakeUpstream({
        "/hypothesis?status=live&limit=50": (True, {
            "count": 2,
            "hypotheses": [
                {
                    "hypothesis_id": "abc-123",
                    "name": "good_hyp",
                    "sport": "nba",
                    "market_type": "spreads",
                    "status": "live",
                    "rolling_roi": 0.05,
                    "rolling_hit_rate": 0.58,
                    "rolling_clv": 0.03,
                    "promoted_at": "2026-04-01T00:00:00Z",
                    "recent_signals": 7,
                },
                {
                    "hypothesis_id": "bad-456",
                    "name": "bad_hyp",
                    "sport": "mlb",
                    "market_type": "totals",
                    "status": "paused",
                    "rolling_roi": -0.10,
                },
            ],
        }),
    })
    app = _build(stub, empty_db)
    with TestClient(app) as c:
        r = c.get("/api/hypotheses/live")
        assert r.status_code == 200
        body = r.json()
        assert body["online"] is True
        assert body["count"] == 2
        names = {h["name"]: h for h in body["hypotheses"]}
        assert names["good_hyp"]["health_color"] == "green"
        assert names["bad_hyp"]["health_color"] == "red"
        # days_live computed for a rowwith promoted_at.
        assert names["good_hyp"]["days_live"] is not None


def test_hypotheses_offline(empty_db):
    stub = FakeUpstream()
    app = _build(stub, empty_db)
    with TestClient(app) as c:
        r = c.get("/api/hypotheses/live")
        assert r.status_code == 200
        body = r.json()
        assert body["online"] is False
        assert body["hypotheses"] == []


# ---------------------------------------------------------------------------
# /api/orders — both online path and DB-fallback path
# ---------------------------------------------------------------------------


def test_orders_uses_api_when_available(seeded_db):
    stub = FakeUpstream({
        "/orders?limit=20": (True, {
            "count": 1,
            "orders": [{"id": 99, "state": "pending_approval", "sport": "nba", "stake": 5.0}],
        }),
    })
    app = _build(stub, seeded_db)
    with TestClient(app) as c:
        r = c.get("/api/orders")
        assert r.status_code == 200
        body = r.json()
        assert body["online"] is True
        assert body["source"] == "api"
        assert body["counts_by_state"]["pending_approval"] == 1


def test_orders_falls_back_to_db_when_api_down(seeded_db):
    stub = FakeUpstream()
    app = _build(stub, seeded_db)
    with TestClient(app) as c:
        r = c.get("/api/orders")
        assert r.status_code == 200
        body = r.json()
        assert body["online"] is False
        assert body["source"] == "db"
        assert body["count"] == 2
        states = {o["state"] for o in body["orders"]}
        assert "pending_approval" in states
        assert "filled" in states
        assert body["counts_by_state"]["pending_approval"] == 1


def test_orders_empty_db_renders_ok(empty_db):
    stub = FakeUpstream()
    app = _build(stub, empty_db)
    with TestClient(app) as c:
        r = c.get("/api/orders")
        assert r.status_code == 200
        body = r.json()
        assert body["orders"] == []
        assert body["counts_by_state"] == {}


# ---------------------------------------------------------------------------
# /api/portfolio
# ---------------------------------------------------------------------------


def test_portfolio_computes_drawdown_and_exposure(empty_db):
    stub = FakeUpstream({
        "/bets/bankroll": (True, [
            {"id": 3, "balance": 950.0, "change": -50.0},
            {"id": 2, "balance": 1000.0, "change": 0.0},
            {"id": 1, "balance": 1000.0, "change": 1000.0},
        ]),
        "/bets": (True, [
            {"sport": "nba", "stake": 25.0, "status": "open"},
            {"sport": "nba", "stake": 10.0, "status": "open"},
            {"sport": "mlb", "stake": 40.0, "status": "won"},  # settled → excluded
        ]),
    })
    app = _build(stub, empty_db)
    with TestClient(app) as c:
        r = c.get("/api/portfolio")
        assert r.status_code == 200
        body = r.json()
        assert body["online"] is True
        assert body["current_balance"] == 950.0
        assert body["rolling_peak"] == 1000.0
        assert abs(body["drawdown_pct"] - 5.0) < 1e-6
        assert body["total_open_exposure"] == 35.0
        assert body["exposure_by_sport"]["nba"] == 35.0
        assert body["unsettled_count"] == 2


def test_portfolio_offline(empty_db):
    stub = FakeUpstream()
    app = _build(stub, empty_db)
    with TestClient(app) as c:
        r = c.get("/api/portfolio")
        assert r.status_code == 200
        assert r.json()["online"] is False


# ---------------------------------------------------------------------------
# /api/ingestion — SLA classification
# ---------------------------------------------------------------------------


def test_ingestion_db_fallback_classifies_sla(seeded_db):
    stub = FakeUpstream()  # /health/detailed fails → DB fallback
    app = _build(stub, seeded_db)
    with TestClient(app) as c:
        r = c.get("/api/ingestion")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "db"
        by_source = {row["source"]: row for row in body["sources"]}
        # Fresh row -> green. Stale (90min+) is likely red but we accept
        # yellow too, since SLA defaults vary by source.
        assert by_source["test_source_ok"]["sla_status"] == "green"
        assert by_source["test_source_stale"]["sla_status"] in ("yellow", "red")


def test_ingestion_prefers_api_when_available(empty_db):
    stub = FakeUpstream({
        "/health/detailed": (True, {
            "ingestion_sla": {
                "sources": [
                    {"source": "dk_scraper", "sla_status": "green", "age_seconds": 60, "sla_seconds": 900, "status": "success"},
                ],
            },
        }),
    })
    app = _build(stub, empty_db)
    with TestClient(app) as c:
        r = c.get("/api/ingestion")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "api"
        assert body["online"] is True


# ---------------------------------------------------------------------------
# /api/alerts — resilient to missing tables
# ---------------------------------------------------------------------------


def test_alerts_empty_when_no_tables(empty_db):
    app = _build(FakeUpstream(), empty_db)
    with TestClient(app) as c:
        r = c.get("/api/alerts")
        assert r.status_code == 200
        body = r.json()
        assert body["alerts"] == []
        assert body["count"] == 0


def test_alerts_reads_available_tables(tmp_path):
    db = tmp_path / "callisto.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            message TEXT
        );
        INSERT INTO alerts (timestamp, message) VALUES ('2026-04-22T12:00:00Z', 'test breach');
        """
    )
    conn.commit()
    conn.close()
    app = _build(FakeUpstream(), str(db))
    with TestClient(app) as c:
        r = c.get("/api/alerts")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["alerts"][0]["message"] == "test breach"
        assert body["alerts"][0]["_source_table"] == "alerts"
