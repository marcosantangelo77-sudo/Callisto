"""Debug/memory + /admin/sql route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singletons (``DB_PATH``,
``_auth_logger``, ``logger``) via a late ``from api import ...`` inside the
function body to avoid a circular import at module load time.
"""

from __future__ import annotations

import gc
import tracemalloc
from typing import Optional

from fastapi import HTTPException, Request

# Module-level tracemalloc baseline — compared across /debug/memory calls.
_tracemalloc_snapshot: Optional[tracemalloc.Snapshot] = None


# ---------------------------------------------------------------------------
# Debug / memory endpoints
# ---------------------------------------------------------------------------

async def debug_memory(_auth: None = None):
    """tracemalloc snapshot comparison — identifies the top growing allocations.

    First call takes a baseline snapshot. Subsequent calls compare against
    the previous snapshot and return the top 30 growing allocations by size.
    Also forces gc.collect() and reports process RSS.
    """
    global _tracemalloc_snapshot
    import psutil

    gc.collect()
    process = psutil.Process()
    rss_mb = process.memory_info().rss / (1024 * 1024)

    if not tracemalloc.is_tracing():
        raise HTTPException(
            status_code=409,
            detail=f"tracemalloc not active — set CALLISTO_TRACEMALLOC=1 and restart to enable (rss_mb={round(rss_mb, 1)})",
        )

    current = tracemalloc.take_snapshot()
    current = current.filter_traces((
        tracemalloc.Filter(False, "<frozen *>"),
        tracemalloc.Filter(False, "<unknown>"),
        tracemalloc.Filter(False, tracemalloc.__file__),
    ))

    result = {
        "rss_mb": round(rss_mb, 1),
        "tracemalloc_traced_mb": round(tracemalloc.get_traced_memory()[0] / (1024 * 1024), 1),
        "tracemalloc_peak_mb": round(tracemalloc.get_traced_memory()[1] / (1024 * 1024), 1),
    }

    if _tracemalloc_snapshot is not None:
        # Compare against previous snapshot — shows what GREW
        stats = current.compare_to(_tracemalloc_snapshot, "lineno")
        result["comparison"] = "vs_previous_snapshot"
        result["top_growth"] = [
            {
                "file": str(stat.traceback),
                "size_kb": round(stat.size / 1024, 1),
                "size_diff_kb": round(stat.size_diff / 1024, 1),
                "count": stat.count,
                "count_diff": stat.count_diff,
            }
            for stat in stats[:30]
        ]
    else:
        # First call — just show current top allocations
        stats = current.statistics("lineno")
        result["comparison"] = "baseline (first call)"
        result["top_allocations"] = [
            {
                "file": str(stat.traceback),
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            }
            for stat in stats[:30]
        ]

    _tracemalloc_snapshot = current
    return result


async def debug_memory_traces(limit: int = 10, _auth: None = None):
    """Show full stack traces for the top memory consumers."""
    if not tracemalloc.is_tracing():
        raise HTTPException(
            status_code=409,
            detail="tracemalloc not active — set CALLISTO_TRACEMALLOC=1 and restart to enable",
        )

    snapshot = tracemalloc.take_snapshot()
    snapshot = snapshot.filter_traces((
        tracemalloc.Filter(False, "<frozen *>"),
        tracemalloc.Filter(False, "<unknown>"),
    ))
    stats = snapshot.statistics("traceback")

    traces = []
    for stat in stats[:limit]:
        traces.append({
            "size_kb": round(stat.size / 1024, 1),
            "count": stat.count,
            "traceback": [str(line) for line in stat.traceback.format()],
        })
    return {"top_traces": traces}


async def debug_gc(_auth: None = None):
    """Force garbage collection and report stats."""
    gc.collect()
    gc.collect()  # Second pass catches ref cycles
    import psutil
    rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
    result = {
        "rss_mb": round(rss_mb, 1),
        "gc_counts": gc.get_count(),
        "gc_stats": gc.get_stats(),
    }
    if tracemalloc.is_tracing():
        result["tracemalloc_traced_mb"] = round(tracemalloc.get_traced_memory()[0] / (1024 * 1024), 1)
    else:
        result["tracemalloc"] = "disabled (set CALLISTO_TRACEMALLOC=1 to enable)"
    return result


# ---------------------------------------------------------------------------
# /admin/sql — read-only validated SQL for debugging
# ---------------------------------------------------------------------------

# PRAGMA allowlist for /admin/sql — read-only diagnostic pragmas only.
# ANY other PRAGMA (writable_schema=1, journal_mode=OFF, foreign_keys=OFF, etc.)
# is rejected. Value assignment to even allowed PRAGMAs is rejected.
_ALLOWED_PRAGMAS = frozenset({
    "integrity_check",
    "quick_check",
    "page_count",
    "page_size",
    "wal_autocheckpoint",
    "wal_checkpoint",
    "schema_version",
    "user_version",
    "cache_size",
    "freelist_count",
    "journal_mode",      # read-only query form
    "database_list",
    "table_info",
    "index_list",
    "index_info",
    "foreign_key_list",
    "compile_options",
})


def validate_admin_sql(sql: str) -> Optional[str]:
    """AST-validate a /admin/sql query. Return None if OK, else error string.

    Rules:
      * exactly one statement (sqlparse must parse to exactly one non-empty stmt)
      * must be SELECT or a whitelisted read-only PRAGMA
      * PRAGMA forbidden if it assigns a value or is not in _ALLOWED_PRAGMAS
      * rejects CTEs whose body contains INSERT/UPDATE/DELETE (write-CTEs)
    """
    try:
        import sqlparse
        from sqlparse.sql import Statement
    except ImportError:
        # Degraded-mode fallback: sqlparse isn't installed. Be extra strict —
        # accept only simple SELECTs with no semicolons and no PRAGMA at all.
        normalized = sql.strip().rstrip(";")
        if ";" in normalized:
            return "Multi-statement queries not allowed"
        if not normalized.upper().startswith("SELECT"):
            return "sqlparse unavailable; only single SELECT allowed in degraded mode"
        forbidden = ("PRAGMA", "DROP", "DELETE", "INSERT", "UPDATE", "ALTER",
                     "CREATE", "ATTACH", "DETACH", "REINDEX", "VACUUM", "REPLACE")
        upper = normalized.upper()
        import re as _re
        for kw in forbidden:
            if _re.search(rf"\b{kw}\b", upper):
                return f"Forbidden keyword: {kw}"
        return None

    parsed = sqlparse.parse(sql)
    # sqlparse may return empty statements for trailing semicolons — filter them.
    real_stmts = [
        s for s in parsed
        if isinstance(s, Statement) and s.tokens and str(s).strip().rstrip(";").strip()
    ]
    if len(real_stmts) == 0:
        return "Empty statement"
    if len(real_stmts) > 1:
        return "Multi-statement queries not allowed"
    stmt = real_stmts[0]
    stmt_type = stmt.get_type()  # 'SELECT', 'PRAGMA', 'UPDATE', 'UNKNOWN', etc.

    # Check for write-verbs anywhere (e.g., hidden inside a WITH ... DELETE CTE).
    # sqlparse doesn't flag these via get_type() when wrapped in a CTE.
    upper_sql = str(stmt).upper()
    import re as _re
    for kw in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
               "ATTACH", "DETACH", "REINDEX", "VACUUM", "REPLACE"):
        if _re.search(rf"\b{kw}\b", upper_sql):
            return f"Forbidden keyword: {kw}"

    if stmt_type == "SELECT":
        return None

    # PRAGMA handling — sqlparse classifies the whole "PRAGMA name[=value]"
    # as a single Identifier token under an UNKNOWN statement type, so we
    # prefix-sniff the raw upper-cased text instead.
    stripped_upper = upper_sql.strip().rstrip(";").strip()
    if stripped_upper.startswith("PRAGMA"):
        # Extract PRAGMA body + check for assignment.
        #   Allowed:  PRAGMA integrity_check;   PRAGMA page_count;
        #   Rejected: PRAGMA writable_schema=1; PRAGMA journal_mode=OFF;
        #             PRAGMA foreign_keys=OFF;
        body = stripped_upper[len("PRAGMA"):].strip()
        if not body:
            return "Empty PRAGMA"
        # Reject any assignment syntax
        if "=" in body:
            return "PRAGMA value assignment not allowed"
        # Reject function-call style with args beyond the trivial form,
        # e.g. PRAGMA wal_checkpoint(TRUNCATE) — keep it very conservative.
        if "(" in body:
            name = body.split("(", 1)[0].strip().lower()
        else:
            name = body.strip().lower()
        if name not in _ALLOWED_PRAGMAS:
            return f"PRAGMA '{name}' not in allowlist"
        return None

    if stmt_type == "UNKNOWN":
        return "Unrecognized statement type; only SELECT and whitelisted PRAGMA allowed"
    return f"Statement type '{stmt_type}' not allowed"


async def admin_sql(request_body: dict, client_host: str = "?"):
    """Read-only SQL query against callisto.db for debugging.

    AST-validated: parses via sqlparse, rejects multi-statement queries,
    write-verbs (even inside CTEs), and any PRAGMA outside a small read-only
    allowlist. Also runs under `PRAGMA query_only = ON` and a 10s timeout.
    """
    import logging
    import re as _re  # noqa: F401  (kept for parity with validator internals)
    import sqlite3 as _sqlite3
    import time as _time

    import aiosqlite

    logger = logging.getLogger("callisto.api")
    from api import DB_PATH, _auth_logger

    sql = (request_body.get("sql") or "").strip()
    if not sql:
        raise HTTPException(status_code=400, detail="No SQL provided")

    err = validate_admin_sql(sql)
    if err:
        _auth_logger.warning(
            "AUTH_ADMIN_SQL_REJECTED host=%s reason=%s sql=%r",
            client_host,
            err,
            sql[:300],
        )
        raise HTTPException(status_code=400, detail=err)

    # 10-second execution budget. sqlite3's progress handler fires every N
    # opcodes; returning non-zero aborts the query cleanly.
    start = _time.monotonic()

    def _timeout_handler():
        if _time.monotonic() - start > 10.0:
            return 1  # abort
        return 0

    raw_conn = None
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA query_only = ON")
            # Attach progress handler on the underlying sqlite3 connection.
            # aiosqlite exposes it via `db._conn`; fall back to leaving it off.
            try:
                raw_conn = getattr(db, "_conn", None)
                if raw_conn is not None:
                    raw_conn.set_progress_handler(_timeout_handler, 10_000)
            except Exception:
                pass
            try:
                cursor = await db.execute(sql)
                rows = await cursor.fetchall()
            except _sqlite3.OperationalError as oe:
                if "interrupted" in str(oe).lower() or "abort" in str(oe).lower():
                    raise HTTPException(status_code=504, detail="Query exceeded 10s timeout")
                raise
            finally:
                try:
                    if raw_conn is not None:
                        raw_conn.set_progress_handler(None, 0)
                except Exception:
                    pass
            cols = [d[0] for d in cursor.description] if cursor.description else []
            return {
                "columns": cols,
                "rows": [list(r) for r in rows[:500]],  # Cap at 500 rows
                "row_count": len(rows),
                "truncated": len(rows) > 500,
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_sql execution failed sql=%r", sql[:300])
        raise HTTPException(status_code=500, detail=str(e))
