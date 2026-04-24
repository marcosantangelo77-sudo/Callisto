"""
Minimal read-only ops dashboard for Callisto.

Exposes a FastAPI sub-app that:
  * Serves static HTML/JS/CSS from ``web/dashboard/``.
  * Exposes a thin JSON layer under ``/api/*`` that aggregates the handful
    of live Callisto endpoints the UI needs, so the browser never has to
    coordinate 6+ cross-origin calls.
  * Reads SQLite tables (``ingestion_runs``, ``orders``, alert logs) that
    don't yet have stable HTTP endpoints on the main API.

Two deployment modes:

1.  **Standalone** — ``scripts/run_dashboard.py`` wraps this module in its
    own ``FastAPI()`` root app and binds a new port (default ``8421``).
2.  **Sub-app** — call ``build_dashboard_subapp()`` and ``app.mount("/dashboard", subapp)``
    from ``api.py`` in a follow-up PR. No behaviour changes here.

**Read-only**: this module MUST NOT issue POST/PUT/DELETE against the main
API. It only reads from Callisto's HTTP surface and the shared SQLite DB.

**Offline-tolerant**: every upstream call is wrapped; if the main API is
down we return ``{"online": false, ...}`` and the UI renders an "offline"
banner rather than crashing.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_MAIN_API = os.environ.get("CALLISTO_MAIN_API", "http://localhost:8420")
DEFAULT_TIMEOUT_S = float(os.environ.get("CALLISTO_DASHBOARD_TIMEOUT", "3.0"))
DASHBOARD_TOKEN = os.environ.get("CALLISTO_DASHBOARD_TOKEN", "")

# Default to the repo-local DB. run_dashboard.py can override via env.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = os.environ.get(
    "CALLISTO_DB_PATH",
    str(_REPO_ROOT / "data" / "callisto.db"),
)

WEB_DIR = _REPO_ROOT / "web" / "dashboard"


# ---------------------------------------------------------------------------
# Upstream HTTP client (main Callisto API)
# ---------------------------------------------------------------------------


class UpstreamClient:
    """Tiny helper around ``httpx.AsyncClient``.

    Every call returns ``(ok, payload)``; never raises outward so the UI
    can stay live even when the main API is unreachable.
    """

    def __init__(self, base_url: str = DEFAULT_MAIN_API, timeout: float = DEFAULT_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, path: str) -> tuple[bool, Any]:
        url = f"{self.base_url}{path}"
        try:
            client = await self._get_client()
            resp = await client.get(url)
            if resp.status_code >= 400:
                return False, {"error": f"upstream {resp.status_code}", "path": path}
            return True, resp.json()
        except Exception as e:  # pragma: no cover — exercised via tests with monkeypatch
            return False, {"error": str(e), "path": path}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _read_ingestion_stats(db_path: str) -> list[dict]:
    """Return per-source latest ingestion row + age_seconds + sla/sla_status.

    SLA thresholds come from ``tools.health.resolve_sla_seconds`` when
    importable, otherwise fall back to a conservative default of 900 s.
    """
    try:
        from tools.health import resolve_sla_seconds, CRITICAL_MULTIPLIER
    except Exception:  # pragma: no cover
        def resolve_sla_seconds(_: str) -> int:
            return 900
        CRITICAL_MULTIPLIER = 3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except Exception:
        return []

    out: list[dict] = []
    try:
        if not _table_exists(conn, "ingestion_runs"):
            return []

        # Latest row per source where finished_at is set.
        rows = conn.execute(
            """
            SELECT source, status, started_at, finished_at, rows_written,
                   (julianday('now') - julianday(finished_at)) * 86400 AS age_s
            FROM ingestion_runs
            WHERE finished_at IS NOT NULL
              AND id IN (
                SELECT MAX(id) FROM ingestion_runs
                WHERE finished_at IS NOT NULL
                GROUP BY source
              )
            ORDER BY source
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    for source, status, started_at, finished_at, rows_written, age_s in rows:
        try:
            age_f = float(age_s) if age_s is not None else None
            sla = resolve_sla_seconds(source)
            if age_f is None:
                sla_status = "unknown"
            elif age_f <= sla:
                sla_status = "green"
            elif age_f <= sla * CRITICAL_MULTIPLIER:
                sla_status = "yellow"
            else:
                sla_status = "red"
        except Exception:
            age_f, sla, sla_status = None, 900, "unknown"

        out.append(
            {
                "source": source,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "rows_written": rows_written,
                "age_seconds": age_f,
                "sla_seconds": sla,
                "sla_status": sla_status,
            }
        )
    return out


def _read_orders(db_path: str, limit: int = 20) -> list[dict]:
    """Best-effort read of the ``orders`` table for the UI.

    Tolerates absence or schema drift — if the table isn't there, returns
    an empty list so the panel renders an empty state rather than 500ing.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except Exception:
        return []

    try:
        if not _table_exists(conn, "orders"):
            return []

        # Discover columns so we don't blow up on schema drift.
        cur = conn.execute("PRAGMA table_info(orders)")
        cols = [row[1] for row in cur.fetchall()]
        if not cols:
            return []

        # ORDER BY id DESC if id exists, else by a plausible timestamp.
        order_col = "id" if "id" in cols else ("created_at" if "created_at" in cols else cols[0])
        select_cols = ", ".join(cols)
        rows = conn.execute(
            f"SELECT {select_cols} FROM orders ORDER BY {order_col} DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    return [dict(zip(cols, row)) for row in rows]


def _count_orders_by_state(db_path: str) -> dict[str, int]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except Exception:
        return {}
    try:
        if not _table_exists(conn, "orders"):
            return {}
        cur = conn.execute("PRAGMA table_info(orders)")
        cols = [row[1] for row in cur.fetchall()]
        state_col = "state" if "state" in cols else ("status" if "status" in cols else None)
        if state_col is None:
            return {}
        rows = conn.execute(
            f"SELECT {state_col}, COUNT(*) FROM orders GROUP BY {state_col}"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(k or "unknown"): int(v) for k, v in rows}


def _read_alerts(db_path: str, limit: int = 20) -> list[dict]:
    """Pull recent alert-ish rows from whichever table happens to exist.

    We check a handful of plausible table names (alerts, alert_log,
    line_alerts, circuit_trips). Missing tables are silently skipped.
    """
    candidates = ["alerts", "alert_log", "line_alerts", "circuit_trips"]
    merged: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except Exception:
        return []
    try:
        for tbl in candidates:
            if not _table_exists(conn, tbl):
                continue
            try:
                cur = conn.execute(f"PRAGMA table_info({tbl})")
                cols = [row[1] for row in cur.fetchall()]
                if not cols:
                    continue
                order_col = "id" if "id" in cols else ("timestamp" if "timestamp" in cols else cols[0])
                select_cols = ", ".join(cols)
                rows = conn.execute(
                    f"SELECT {select_cols} FROM {tbl} ORDER BY {order_col} DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                for row in rows:
                    d = dict(zip(cols, row))
                    d["_source_table"] = tbl
                    merged.append(d)
            except sqlite3.Error:
                continue
    finally:
        conn.close()

    # Crude cross-table sort: newest first by any timestamp/created_at field
    def _key(d: dict) -> str:
        return str(d.get("timestamp") or d.get("created_at") or d.get("tripped_at") or "")

    merged.sort(key=_key, reverse=True)
    return merged[:limit]


# ---------------------------------------------------------------------------
# Sub-app factory
# ---------------------------------------------------------------------------


def build_dashboard_subapp(
    main_api_url: str = DEFAULT_MAIN_API,
    db_path: str = DEFAULT_DB_PATH,
) -> FastAPI:
    """Return a FastAPI app that can run standalone OR be mounted.

    Passing ``main_api_url`` and ``db_path`` explicitly lets tests inject
    fakes without touching the environment.
    """
    app = FastAPI(
        title="Callisto Ops Dashboard",
        version="0.1.0",
        docs_url=None,          # no interactive docs; this is an internal UI
        redoc_url=None,
        openapi_url=None,
    )
    app.state.client = UpstreamClient(base_url=main_api_url)
    app.state.db_path = db_path

    def _client() -> "UpstreamClient | FakeLike":
        # Handlers pull the client through app.state every call so tests
        # can inject a stub by assigning to app.state.client.
        return app.state.client

    # --- Optional token gate -------------------------------------------------
    # Loopback-allowed like the main admin gate. If CALLISTO_DASHBOARD_TOKEN
    # is set, non-loopback requests must present it as X-Dashboard-Token or
    # ?token=. Loopback (127.0.0.1 / ::1) is always allowed — the dashboard
    # is meant for Marco's local machine.

    @app.middleware("http")
    async def _token_gate(request: Request, call_next):
        if not DASHBOARD_TOKEN:
            return await call_next(request)
        host = request.client.host if request.client else ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return await call_next(request)
        token = request.headers.get("X-Dashboard-Token") or request.query_params.get("token") or ""
        # SECURITY (audit 2026-04-23): timing-safe comparison. Plain `!=`
        # leaks match-prefix length via timing on a shared-host deploy.
        import secrets as _secrets
        if not _secrets.compare_digest(token, DASHBOARD_TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    # --- Lifespan ------------------------------------------------------------
    @app.on_event("shutdown")
    async def _close():  # pragma: no cover — exercised by integration only
        c = app.state.client
        close = getattr(c, "close", None)
        if close is not None:
            await close()

    # --- Static site --------------------------------------------------------
    if WEB_DIR.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(WEB_DIR)),
            name="dashboard-static",
        )

    @app.get("/", include_in_schema=False)
    async def _index():
        index = WEB_DIR / "index.html"
        if not index.exists():
            raise HTTPException(status_code=500, detail=f"index.html missing at {index}")
        return FileResponse(str(index))

    # --- JSON API (thin proxy + DB reads) -----------------------------------

    @app.get("/api/ping")
    async def ping():
        """Liveness for the dashboard itself — not the main API."""
        return {"ok": True, "ts": time.time()}

    @app.get("/api/status")
    async def status():
        """
        Aggregated live-state payload — everything the top panel needs in one
        call so the UI doesn't fan out 5 times per refresh.
        """
        c = _client()
        tasks = [
            c.get("/system/full-status"),
            c.get("/health"),
            c.get("/executor/status"),
            c.get("/claude/status"),
        ]
        (ok_full, full), (ok_health, health), (ok_exec, executor), (ok_claude, claude) = \
            await asyncio.gather(*tasks)

        online = ok_full or ok_health
        return {
            "online": online,
            "fetched_at": time.time(),
            "full_status": full if ok_full else None,
            "health": health if ok_health else None,
            "executor": executor if ok_exec else None,
            "claude": claude if ok_claude else None,
            "errors": {
                "full_status": None if ok_full else full,
                "health": None if ok_health else health,
                "executor": None if ok_exec else executor,
                "claude": None if ok_claude else claude,
            },
        }

    @app.get("/api/hypotheses/live")
    async def hypotheses_live(limit: int = 50):
        """LIVE hypotheses with a lightweight health classification."""
        ok, payload = await _client().get(f"/hypothesis?status=live&limit={limit}")
        if not ok:
            return {"online": False, "count": 0, "hypotheses": [], "error": payload}
        hyps = payload.get("hypotheses", []) if isinstance(payload, dict) else []
        shaped: list[dict] = []
        for h in hyps:
            shaped.append(_shape_hypothesis(h))
        return {"online": True, "count": len(shaped), "hypotheses": shaped}

    @app.get("/api/orders")
    async def orders(limit: int = 20):
        """
        Prefer the live API's ``/orders`` endpoint; fall back to a direct DB
        read if the API is down (so Marco can still see pending approvals
        during an API outage).
        """
        ok, payload = await _client().get(f"/orders?limit={limit}")
        if ok and isinstance(payload, dict):
            orders_list = payload.get("orders", [])
            counts = _counts_from_orders_list(orders_list)
            return {
                "online": True,
                "source": "api",
                "count": len(orders_list),
                "orders": orders_list,
                "counts_by_state": counts,
            }
        orders_list = _read_orders(app.state.db_path, limit=limit)
        counts = _count_orders_by_state(app.state.db_path)
        return {
            "online": False,
            "source": "db",
            "count": len(orders_list),
            "orders": orders_list,
            "counts_by_state": counts,
            "error": payload if not ok else None,
        }

    @app.get("/api/portfolio")
    async def portfolio():
        c = _client()
        ok_br, bankroll = await c.get("/bets/bankroll")
        ok_bets, bets = await c.get("/bets")
        if not ok_br and not ok_bets:
            return {"online": False, "error": bankroll}

        # bankroll endpoint returns a list of ledger entries newest-first
        current_balance: Optional[float] = None
        rolling_peak: Optional[float] = None
        if isinstance(bankroll, list) and bankroll:
            try:
                balances = [float(r.get("balance")) for r in bankroll if r.get("balance") is not None]
                current_balance = balances[0] if balances else None
                rolling_peak = max(balances) if balances else None
            except Exception:
                pass

        drawdown_pct: Optional[float] = None
        if current_balance is not None and rolling_peak and rolling_peak > 0:
            drawdown_pct = (rolling_peak - current_balance) / rolling_peak * 100.0

        # Open exposure = sum of unsettled stakes by sport and overall
        exposure_by_sport: dict[str, float] = {}
        total_exposure = 0.0
        unsettled_count = 0
        if isinstance(bets, list):
            for b in bets:
                if (b.get("resolved") or b.get("status") in ("won", "lost", "push", "settled")):
                    continue
                stake = float(b.get("stake") or 0.0)
                sport = str(b.get("sport") or "unknown")
                exposure_by_sport[sport] = exposure_by_sport.get(sport, 0.0) + stake
                total_exposure += stake
                unsettled_count += 1

        return {
            "online": True,
            "current_balance": current_balance,
            "rolling_peak": rolling_peak,
            "drawdown_pct": drawdown_pct,
            "total_open_exposure": total_exposure,
            "unsettled_count": unsettled_count,
            "exposure_by_sport": exposure_by_sport,
        }

    @app.get("/api/ingestion")
    async def ingestion():
        """
        Per-source ingestion telemetry. Prefer ``/health/detailed`` on the
        main API (which pulls from ``tools.ingestion_observability``);
        fall back to a direct ``ingestion_runs`` read when the API is down.
        """
        ok, payload = await _client().get("/health/detailed")
        if ok and isinstance(payload, dict):
            sla = payload.get("ingestion_sla") or {}
            if sla and not sla.get("unavailable"):
                return {"online": True, "source": "api", "sla_report": sla}
        # Fallback: read the DB directly.
        rows = _read_ingestion_stats(app.state.db_path)
        return {"online": False, "source": "db", "sources": rows}

    @app.get("/api/alerts")
    async def alerts(limit: int = 20):
        # Best-effort. Alerts table names vary across migrations.
        items = _read_alerts(app.state.db_path, limit=limit)
        return {"count": len(items), "alerts": items}

    @app.get("/api/metrics")
    async def metrics_snapshot():
        """Thin proxy to the main API's /metrics/json for the ops panels."""
        ok, payload = await _client().get("/metrics/json")
        if not ok:
            return {"online": False, "error": payload, "metrics": []}
        if not isinstance(payload, dict):
            return {"online": False, "error": {"reason": "malformed_payload"}, "metrics": []}
        return {"online": True, **payload}

    @app.get("/api/db/health")
    async def db_health():
        ok, payload = await _client().get("/admin/db/health")
        if not ok:
            return {"online": False, "error": payload}
        return {"online": True, **(payload if isinstance(payload, dict) else {})}

    @app.get("/api/db/migrations")
    async def db_migrations():
        ok, payload = await _client().get("/admin/db/migrations")
        if not ok:
            return {"online": False, "error": payload}
        return {"online": True, **(payload if isinstance(payload, dict) else {})}

    @app.get("/api/scrapers/health")
    async def scrapers_health_api():
        ok, payload = await _client().get("/odds/scrapers/health")
        if not ok:
            return {"online": False, "error": payload, "scrapers": []}
        return {"online": True, **(payload if isinstance(payload, dict) else {})}

    @app.get("/api/risk-report")
    async def risk_report():
        ok, payload = await _client().get("/bets/risk-report")
        if not ok:
            return {"online": False, "error": payload}
        return {"online": True, **(payload if isinstance(payload, dict) else {})}

    @app.get("/api/eligibility")
    async def eligibility():
        """Surface the hypothesis-gen eligibility block from /system/full-status."""
        ok, payload = await _client().get("/system/full-status")
        if not ok or not isinstance(payload, dict):
            return {"online": False, "error": payload if not ok else "malformed"}
        auto = payload.get("autonomous_loop") or {}
        elig = auto.get("eligibility") if isinstance(auto, dict) else None
        return {
            "online": True,
            "eligibility": elig,
            "research_sports": (auto or {}).get("research_sports") if isinstance(auto, dict) else None,
        }

    @app.get("/api/health/deep")
    async def health_deep_proxy():
        ok, payload = await _client().get("/health/deep")
        if not ok:
            return {"online": False, "error": payload}
        return {"online": True, **(payload if isinstance(payload, dict) else {})}

    @app.get("/api/tasks")
    async def tasks_list(limit: int = 25):
        ok, payload = await _client().get(f"/tasks?limit={limit}")
        if not ok:
            return {"online": False, "error": payload, "tasks": []}
        if isinstance(payload, dict):
            return {"online": True, **payload}
        if isinstance(payload, list):
            return {"online": True, "tasks": payload, "count": len(payload)}
        return {"online": True, "tasks": [], "count": 0}

    return app


def _counts_from_orders_list(orders_list: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in orders_list:
        state = str(o.get("state") or o.get("status") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    return counts


def _shape_hypothesis(h: dict) -> dict:
    """Trim a hypothesis row to the fields the LIVE panel actually renders."""
    promoted_at = h.get("promoted_at") or h.get("updated_at")
    days_live: Optional[float] = None
    if promoted_at:
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(str(promoted_at).replace("Z", "+00:00"))
            days_live = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
        except Exception:
            days_live = None

    recent_hit_rate = h.get("rolling_hit_rate") or h.get("hit_rate")
    recent_roi = h.get("rolling_roi") or h.get("roi")
    recent_clv = h.get("rolling_clv") or h.get("clv")
    recent_signals = h.get("recent_signals") or h.get("signals_last_7d")

    # Health color heuristic. Err on "unknown" rather than guessing green.
    status = str(h.get("status") or "")
    color = "yellow"
    if status in ("paused", "rejected", "retired"):
        color = "red"
    elif isinstance(recent_roi, (int, float)):
        if recent_roi > 0.02:
            color = "green"
        elif recent_roi < -0.02:
            color = "red"

    return {
        "hypothesis_id": h.get("hypothesis_id"),
        "name": h.get("name"),
        "sport": h.get("sport"),
        "market_type": h.get("market_type"),
        "status": status,
        "promoted_at": promoted_at,
        "days_live": days_live,
        "recent_signals": recent_signals,
        "rolling_hit_rate": recent_hit_rate,
        "rolling_roi": recent_roi,
        "rolling_clv": recent_clv,
        "health_color": color,
    }
