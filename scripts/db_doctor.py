#!/usr/bin/env python
"""Callisto DB doctor — manual deep-maintenance script.

Runs against the live callisto.db file (path resolved via
``tools.state_paths.db_path``). Intended for interactive use; the user
runs this when /admin/db/health shows fragmentation or the integrity
check after an unclean shutdown is desired.

Operations
----------
  --integrity-check   Run PRAGMA integrity_check. Read-only.
  --fk-check          Run PRAGMA foreign_key_check. Read-only.
  --truncate          Run PRAGMA wal_checkpoint(TRUNCATE).
  --vacuum            Run VACUUM (rewrites the DB; NEVER against the
                      live API — stop the API or use --allow-live).
  --all               Run everything except VACUUM.
  --json              Emit results as JSON on stdout.
  --db PATH           Override DB path (default: CALLISTO_DB_PATH or
                      memory/callisto.db).

Exits 0 on success, 1 on any operation error, 2 if the DB file is not
found. The script opens short-lived autocommit connections so it is safe
to run alongside read clients; VACUUM takes the write lock and will
contend with the API if it is up.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# Make the tools/ package importable when run from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.state_paths import db_path as _default_db_path  # noqa: E402


def _open(db_file: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 120000")
    return conn


def integrity_check(db_file: str) -> dict:
    t0 = time.monotonic()
    conn = _open(db_file)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        values = [r[0] for r in rows]
        ok = values == ["ok"]
        return {
            "ok": ok,
            "duration_s": round(time.monotonic() - t0, 3),
            "rows": values[:50],
            "truncated": len(values) > 50,
        }
    finally:
        conn.close()


def fk_check(db_file: str) -> dict:
    t0 = time.monotonic()
    conn = _open(db_file)
    try:
        rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        return {
            "ok": not rows,
            "duration_s": round(time.monotonic() - t0, 3),
            "violations": [list(r) for r in rows[:50]],
            "violation_count": len(rows),
            "truncated": len(rows) > 50,
        }
    finally:
        conn.close()


def wal_truncate(db_file: str) -> dict:
    t0 = time.monotonic()
    conn = _open(db_file)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy, log_pages, checkpointed = row if row else (None, None, None)
        return {
            "ok": not busy,
            "busy": busy,
            "log_pages_remaining": log_pages,
            "checkpointed": checkpointed,
            "duration_s": round(time.monotonic() - t0, 3),
        }
    finally:
        conn.close()


def stats(db_file: str) -> dict:
    conn = _open(db_file)
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        ratio = (freelist / page_count) if page_count else 0.0
        return {
            "page_size": page_size,
            "page_count": page_count,
            "freelist_pages": freelist,
            "fragmentation_ratio": round(ratio, 4),
            "db_size_mb": round((page_size * page_count) / (1024 * 1024), 3),
            "journal_mode": journal,
        }
    finally:
        conn.close()


def vacuum(db_file: str, allow_live: bool = False) -> dict:
    if not allow_live:
        api_running = _looks_live()
        if api_running:
            return {
                "ok": False,
                "skipped": True,
                "reason": (
                    "API appears to be running (port 8420 answered). Pass "
                    "--allow-live to override; VACUUM contends with the writer."
                ),
            }
    t0 = time.monotonic()
    conn = _open(db_file)
    try:
        conn.execute("VACUUM")
        return {
            "ok": True,
            "duration_s": round(time.monotonic() - t0, 3),
        }
    finally:
        conn.close()


def _looks_live() -> bool:
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            return s.connect_ex(("127.0.0.1", 8420)) == 0
        finally:
            s.close()
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Callisto DB doctor")
    ap.add_argument("--db", default=None, help="DB path override")
    ap.add_argument("--integrity-check", action="store_true")
    ap.add_argument("--fk-check", action="store_true")
    ap.add_argument("--truncate", action="store_true")
    ap.add_argument("--vacuum", action="store_true")
    ap.add_argument("--all", action="store_true", help="All except VACUUM")
    ap.add_argument("--allow-live", action="store_true", help="Allow VACUUM while API is running")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args(argv)

    db_file = args.db or _default_db_path()
    if not os.path.exists(db_file):
        msg = f"DB file not found: {db_file}"
        if args.json:
            print(json.dumps({"error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 2

    do_any = any([
        args.integrity_check, args.fk_check, args.truncate, args.vacuum, args.all,
    ])
    if not do_any:
        args.all = True

    report: dict = {"db_path": db_file, "stats_before": stats(db_file)}
    had_error = False

    try:
        if args.integrity_check or args.all:
            r = integrity_check(db_file)
            report["integrity_check"] = r
            had_error = had_error or not r["ok"]

        if args.fk_check or args.all:
            r = fk_check(db_file)
            report["fk_check"] = r
            had_error = had_error or not r["ok"]

        if args.truncate or args.all:
            r = wal_truncate(db_file)
            report["wal_truncate"] = r
            had_error = had_error or not r["ok"]

        if args.vacuum:
            r = vacuum(db_file, allow_live=args.allow_live)
            report["vacuum"] = r
            if not r.get("skipped"):
                had_error = had_error or not r["ok"]

        report["stats_after"] = stats(db_file)
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"
        had_error = True

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _pretty(report)

    return 1 if had_error else 0


def _pretty(report: dict) -> None:
    print(f"DB: {report['db_path']}")
    sb = report.get("stats_before", {})
    sa = report.get("stats_after", {})
    print(f"  Before: {sb.get('db_size_mb')} MB, {sb.get('page_count')} pages, "
          f"freelist={sb.get('freelist_pages')} ({sb.get('fragmentation_ratio')} frag)")
    if "integrity_check" in report:
        r = report["integrity_check"]
        print(f"  integrity_check: ok={r['ok']} ({r['duration_s']}s)")
        if not r["ok"]:
            for row in r.get("rows", []):
                print(f"    - {row}")
    if "fk_check" in report:
        r = report["fk_check"]
        print(f"  foreign_key_check: ok={r['ok']} violations={r['violation_count']} "
              f"({r['duration_s']}s)")
        for v in r.get("violations", []):
            print(f"    - {v}")
    if "wal_truncate" in report:
        r = report["wal_truncate"]
        print(f"  wal_checkpoint(TRUNCATE): ok={r['ok']} busy={r['busy']} "
              f"remaining_pages={r['log_pages_remaining']} ({r['duration_s']}s)")
    if "vacuum" in report:
        r = report["vacuum"]
        if r.get("skipped"):
            print(f"  VACUUM skipped: {r.get('reason')}")
        else:
            print(f"  VACUUM: ok={r['ok']} ({r.get('duration_s')}s)")
    print(f"  After:  {sa.get('db_size_mb')} MB, {sa.get('page_count')} pages, "
          f"freelist={sa.get('freelist_pages')} ({sa.get('fragmentation_ratio')} frag)")
    if "error" in report:
        print(f"ERROR: {report['error']}")


if __name__ == "__main__":
    raise SystemExit(main())
