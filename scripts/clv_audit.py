"""Audit CLV accuracy across bets, paper_trades, and clv_log.

Copies the live DB to a temp path (read-only access pattern) and reports:
  * CLV distribution by sport / book / market
  * Suspicious CLV rows (> +200 bps or < -200 bps) — manual review candidates
  * Player-prop paper_trades with actual_stat=NULL but clv_implied populated
    — the classic "0-stat bet with erroneous CLV" pattern
  * Closing-line coverage: how many rows are within the close window vs
    pregame snapshots dressed up as closes

Never mutates the live DB. Safe to run while the API is up.

Usage:
    python scripts/clv_audit.py [/path/to/callisto.db] [--json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


SUSPICIOUS_BPS_HI = 200.0
SUSPICIOUS_BPS_LO = -200.0


def _copy_db(src: str) -> str:
    """Copy live DB + WAL/SHM into a tempdir and return the copied path.

    Caller is responsible for cleanup — returned path lives in a temp dir
    that survives until the process exits.
    """
    tmp = Path(tempfile.mkdtemp(prefix="clv_audit_"))
    dst = tmp / "live.db"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(src + suffix)
        if side.exists():
            shutil.copy2(side, tmp / f"live.db{suffix}")
    return str(dst)


def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return any(row[1] == col for row in cur.fetchall())
    except sqlite3.Error:
        return False


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    )
    return cur.fetchone() is not None


def _distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    s = sorted(values)
    n = len(s)

    def pct(p: float) -> float:
        if n == 1:
            return s[0]
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return s[lo] + (s[hi] - s[lo]) * frac

    mean = sum(s) / n
    return {
        "n": n,
        "mean": round(mean, 2),
        "median": round(pct(0.5), 2),
        "p10": round(pct(0.10), 2),
        "p90": round(pct(0.90), 2),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
        "pct_positive": round(sum(1 for v in s if v > 0) / n * 100, 1),
    }


def distribution_by_dim(
    conn: sqlite3.Connection, dim: str
) -> dict[str, dict[str, float]]:
    """CLV distribution grouped by a single dimension (sport/book/market).

    Prefers `clv_log` rows (cleaner: only resolved bets, canonicalized book).
    Falls back gracefully if the table or the requested column is absent.
    """
    if not _table_exists(conn, "clv_log"):
        return {}
    col_map = {"sport": None, "book": "book", "market": None}
    if dim not in col_map:
        return {"error": f"unknown dim {dim}"}

    if dim == "book":
        if not _has_column(conn, "clv_log", "book"):
            return {}
        rows = conn.execute(
            "SELECT book, clv_prob_bp FROM clv_log "
            "WHERE clv_prob_bp IS NOT NULL "
            "AND actual_result IN ('won', 'lost') "
            "AND close_reliable = 1"
        ).fetchall()
    elif dim == "sport":
        # clv_log has no sport column; join through paper_trades + bets.
        rows = []
        if _table_exists(conn, "paper_trades"):
            rows.extend(
                conn.execute(
                    "SELECT pt.sport, cl.clv_prob_bp FROM clv_log cl "
                    "JOIN paper_trades pt "
                    "  ON cl.bet_id = 'pt:' || pt.trade_id "
                    "WHERE cl.clv_prob_bp IS NOT NULL "
                    "AND cl.actual_result IN ('won', 'lost')"
                ).fetchall()
            )
        if _table_exists(conn, "bets"):
            rows.extend(
                conn.execute(
                    "SELECT b.sport, cl.clv_prob_bp FROM clv_log cl "
                    "JOIN bets b ON cl.bet_id = CAST(b.id AS TEXT) "
                    "WHERE cl.clv_prob_bp IS NOT NULL "
                    "AND cl.actual_result IN ('won', 'lost')"
                ).fetchall()
            )
    elif dim == "market":
        rows = []
        if _table_exists(conn, "paper_trades"):
            rows.extend(
                conn.execute(
                    "SELECT pt.market, cl.clv_prob_bp FROM clv_log cl "
                    "JOIN paper_trades pt "
                    "  ON cl.bet_id = 'pt:' || pt.trade_id "
                    "WHERE cl.clv_prob_bp IS NOT NULL "
                    "AND cl.actual_result IN ('won', 'lost')"
                ).fetchall()
            )
        if _table_exists(conn, "bets"):
            rows.extend(
                conn.execute(
                    "SELECT b.market, cl.clv_prob_bp FROM clv_log cl "
                    "JOIN bets b ON cl.bet_id = CAST(b.id AS TEXT) "
                    "WHERE cl.clv_prob_bp IS NOT NULL "
                    "AND cl.actual_result IN ('won', 'lost')"
                ).fetchall()
            )

    buckets: dict[str, list[float]] = defaultdict(list)
    for key, val in rows:
        buckets[key or "(none)"].append(float(val))

    return {k: _distribution(v) for k, v in sorted(buckets.items())}


def suspicious_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return clv_log rows whose CLV is outside [-200, +200] bps.

    Outside that band for a half-vig-devigged prob-bp computation is the
    signature of either (a) a stale closing line misidentified as the
    close, (b) wrong side-match, or (c) a mis-matched line on a prop.
    """
    if not _table_exists(conn, "clv_log"):
        return []
    rows = conn.execute(
        "SELECT bet_id, event, outcome, book, clv_prob_bp, actual_result, "
        "actual_pnl, close_reliable, logged_at "
        "FROM clv_log "
        "WHERE clv_prob_bp IS NOT NULL "
        "AND (clv_prob_bp > ? OR clv_prob_bp < ?) "
        "ORDER BY ABS(clv_prob_bp) DESC "
        "LIMIT 500",
        (SUSPICIOUS_BPS_HI, SUSPICIOUS_BPS_LO),
    ).fetchall()
    cols = ["bet_id", "event", "outcome", "book", "clv_prob_bp",
            "actual_result", "actual_pnl", "close_reliable", "logged_at"]
    return [dict(zip(cols, r)) for r in rows]


def zero_stat_erroneous(conn: sqlite3.Connection) -> dict[str, Any]:
    """Count paper_trades where the player stat never ingested but a CLV got written.

    Signature:
      - market LIKE 'player_%' OR player IS NOT NULL
      - actual_stat IS NULL (ground truth missing)
      - clv_implied IS NOT NULL (CLV was written anyway — this is the bug)

    Reports affected trade_ids + distinct hypothesis_ids so the caller can
    decide whether to null-out clv_implied retroactively or just let the
    going-forward fix prevent new false CLVs.
    """
    if not _table_exists(conn, "paper_trades"):
        return {"count": 0, "hypothesis_ids": [], "trade_ids": []}

    rows = conn.execute(
        "SELECT trade_id, hypothesis_id, sport, player, market, line, "
        "side, signal_implied_prob, closing_implied, clv_implied, "
        "actual_result "
        "FROM paper_trades "
        "WHERE actual_stat IS NULL "
        "AND clv_implied IS NOT NULL "
        "AND (market LIKE 'player_%' OR player IS NOT NULL) "
        "ORDER BY hypothesis_id"
    ).fetchall()

    cols = ["trade_id", "hypothesis_id", "sport", "player", "market", "line",
            "side", "signal_implied_prob", "closing_implied", "clv_implied",
            "actual_result"]
    examples = [dict(zip(cols, r)) for r in rows[:25]]

    hyp_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        hyp_counts[r[1] or "(none)"] += 1

    return {
        "count": len(rows),
        "distinct_hypotheses": len(hyp_counts),
        "top_hypotheses": sorted(
            hyp_counts.items(), key=lambda kv: kv[1], reverse=True
        )[:10],
        "examples": examples,
    }


def close_window_coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    """How many closing_lines rows were actually captured within the window?"""
    if not _table_exists(conn, "closing_lines"):
        return {"total": 0}
    total = conn.execute("SELECT COUNT(*) FROM closing_lines").fetchone()[0]
    if not _has_column(conn, "closing_lines", "within_close_window"):
        return {
            "total": total,
            "note": "closing_lines.within_close_window column not yet present — "
                    "pre-hardening DB schema. Re-initialize CLVTracker to ALTER.",
        }
    within = conn.execute(
        "SELECT COUNT(*) FROM closing_lines WHERE within_close_window = 1"
    ).fetchone()[0]
    by_source = conn.execute(
        "SELECT source, COUNT(*), "
        "SUM(CASE WHEN within_close_window = 1 THEN 1 ELSE 0 END) "
        "FROM closing_lines GROUP BY source "
        "ORDER BY 2 DESC LIMIT 15"
    ).fetchall()
    return {
        "total": total,
        "within_window": within,
        "within_window_pct": round(within / total * 100, 1) if total else 0,
        "by_source": [
            {"source": s, "total": t, "within_window": w}
            for s, t, w in by_source
        ],
    }


def side_line_mismatch_probe(conn: sqlite3.Connection) -> dict[str, Any]:
    """Detect paper_trades whose closing_odds joined across different lines.

    A trade placed at line=5.5 that picked up closing_odds from a line=6.5
    snapshot will show up here. Uses the bet's own `line` column against
    closing_lines candidates at the same event/market/side.
    """
    if not (_table_exists(conn, "paper_trades") and _table_exists(conn, "closing_lines")):
        return {"count": 0}
    # The canonical fix keyed closing_lines by (event,market,team,line).
    # Pre-fix rows will have line=NULL in closing_lines. The heuristic:
    # if the paper_trade has a numeric line but the chosen closing snapshot
    # has a different numeric line and non-NULL, flag it.
    if not _has_column(conn, "closing_lines", "line"):
        return {"count": 0, "note": "closing_lines.line column absent on this DB"}
    rows = conn.execute(
        "SELECT pt.trade_id, pt.event_id, pt.market, pt.side, pt.line, "
        "pt.closing_odds, cl.line, cl.closing_odds "
        "FROM paper_trades pt "
        "JOIN closing_lines cl "
        "  ON cl.event_id = pt.event_id "
        "  AND cl.market = pt.market "
        "  AND LOWER(cl.team) = LOWER(pt.side) "
        "WHERE pt.line IS NOT NULL "
        "AND cl.line IS NOT NULL "
        "AND ABS(pt.line - cl.line) > 0.5 "
        "AND pt.closing_odds = cl.closing_odds "
        "LIMIT 100"
    ).fetchall()
    cols = ["trade_id", "event_id", "market", "side", "pt_line",
            "pt_closing_odds", "cl_line", "cl_closing_odds"]
    return {
        "count": len(rows),
        "examples": [dict(zip(cols, r)) for r in rows[:15]],
    }


def run_audit(db_path: str) -> dict[str, Any]:
    """Run all audit checks against a read-only copy of db_path."""
    copy = _copy_db(db_path)
    conn = sqlite3.connect(copy)
    try:
        total_clv = 0
        if _table_exists(conn, "clv_log"):
            total_clv = conn.execute(
                "SELECT COUNT(*) FROM clv_log WHERE clv_prob_bp IS NOT NULL"
            ).fetchone()[0]

        susp = suspicious_rows(conn)
        return {
            "db_path": db_path,
            "total_clv_log_rows": total_clv,
            "suspicious_count": len(susp),
            "suspicious_sample": susp[:25],
            "distribution_by_sport": distribution_by_dim(conn, "sport"),
            "distribution_by_book": distribution_by_dim(conn, "book"),
            "distribution_by_market": distribution_by_dim(conn, "market"),
            "zero_stat_erroneous": zero_stat_erroneous(conn),
            "close_window_coverage": close_window_coverage(conn),
            "side_line_mismatch_probe": side_line_mismatch_probe(conn),
        }
    finally:
        conn.close()


def format_text(report: dict[str, Any]) -> str:
    out: list[str] = []
    out.append(f"CLV audit — {report.get('db_path', '?')}")
    out.append(f"Total clv_log rows with CLV: {report.get('total_clv_log_rows', 0)}")

    susp_n = report.get("suspicious_count", 0)
    out.append(
        f"Suspicious rows (|clv|>200bps): {susp_n} "
        f"— review scripts/clv_audit.py --json for details"
    )

    cov = report.get("close_window_coverage", {})
    out.append(
        f"Close-window coverage: {cov.get('within_window', 0)}/{cov.get('total', 0)} "
        f"({cov.get('within_window_pct', 0)}% within 30min window)"
    )

    zero = report.get("zero_stat_erroneous", {})
    out.append(
        f"0-stat bets with erroneous CLV: {zero.get('count', 0)} "
        f"across {zero.get('distinct_hypotheses', 0)} hypotheses"
    )
    top = zero.get("top_hypotheses") or []
    if top:
        out.append("  Top hypothesis_ids:")
        for hid, n in top[:5]:
            out.append(f"    - {hid}: {n}")

    mismatch = report.get("side_line_mismatch_probe", {})
    out.append(f"Side/line mismatch probe: {mismatch.get('count', 0)} suspect rows")

    out.append("")
    out.append("Distribution by sport:")
    for sport, dist in (report.get("distribution_by_sport") or {}).items():
        if dist.get("n", 0) == 0:
            continue
        out.append(
            f"  {sport:20s}  n={dist['n']:5d}  mean={dist.get('mean', 0):7.1f}bp  "
            f"median={dist.get('median', 0):7.1f}bp  +%={dist.get('pct_positive', 0):5.1f}"
        )
    out.append("")
    out.append("Distribution by book:")
    for book, dist in (report.get("distribution_by_book") or {}).items():
        if dist.get("n", 0) == 0:
            continue
        out.append(
            f"  {book:20s}  n={dist['n']:5d}  mean={dist.get('mean', 0):7.1f}bp  "
            f"median={dist.get('median', 0):7.1f}bp  +%={dist.get('pct_positive', 0):5.1f}"
        )
    out.append("")
    out.append("Distribution by market:")
    for mkt, dist in (report.get("distribution_by_market") or {}).items():
        if dist.get("n", 0) == 0:
            continue
        out.append(
            f"  {mkt:20s}  n={dist['n']:5d}  mean={dist.get('mean', 0):7.1f}bp  "
            f"median={dist.get('median', 0):7.1f}bp  +%={dist.get('pct_positive', 0):5.1f}"
        )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit CLV accuracy in Callisto DB")
    parser.add_argument(
        "db_path", nargs="?",
        default="memory/callisto.db",
        help="Path to callisto.db (default: memory/callisto.db)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of human-readable text",
    )
    args = parser.parse_args()

    if not Path(args.db_path).exists():
        print(f"ERROR: db not found: {args.db_path}", file=sys.stderr)
        return 2

    report = run_audit(args.db_path)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
