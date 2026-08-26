"""`callisto status` — hypothesis-pool / lifecycle counts from the local DB."""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _db_path() -> str:
    """Resolve through the entry script so tests that patch
    ``callisto._db_path`` stay effective."""
    import callisto
    return callisto._db_path()


def _default_db_path() -> str:
    return os.getenv("CALLISTO_DB_PATH",
                     str(REPO / "memory" / "callisto.db"))


def _print_appliance_switches() -> None:
    """Print bind host + money/signal env switches (env only, informational)."""
    bind_host = os.getenv("CALLISTO_BIND_HOST", "").strip() or "127.0.0.1"
    print("=== APPLIANCE SWITCHES ===")
    print(f"  bind host: {bind_host}")
    for name in ("CALLISTO_LOCAL_ONLY",
                 "CALLISTO_ALLOW_LIVE_EXECUTE",
                 "CALLISTO_ALLOW_SIGNAL_REFRESH"):
        state = "on" if os.getenv(name, "").strip() else "off"
        short = name.removeprefix("CALLISTO_")
        print(f"  {short}: {state}")


def cmd_status(args: argparse.Namespace) -> int:
    _print_appliance_switches()

    db = _db_path()
    if not Path(db).exists():
        print(f"\nno database at {db} — nothing has run on this machine yet")
        return 0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "hypotheses" not in tables:
        print(f"database : {db}")
        print("  (no hypotheses table yet — the lifecycle has not run on "
              "this machine; nothing to report)")
        conn.close()
        return 0

    print(f"database : {db}")
    print("\n=== HYPOTHESIS LIFECYCLE ===")
    rows = list(c.execute(
        "SELECT status, COUNT(*) AS n FROM hypotheses GROUP BY status"))
    if not rows:
        print("  (no hypotheses)")
    for r in rows:
        print(f"  {r['status']:<14} {r['n']}")

    print("\n=== TOP BACKTESTING (by signals) ===")
    rows = list(c.execute("""
        SELECT h.name, h.sport, h.market_type,
               COUNT(DISTINCT be.id) AS events,
               SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) AS sig,
               AVG(be.edge) AS avg_edge
        FROM hypotheses h JOIN backtest_events be
          ON h.hypothesis_id = be.hypothesis_id
        WHERE h.status='backtesting'
        GROUP BY h.hypothesis_id ORDER BY sig DESC LIMIT 10"""))
    for r in rows:
        rate = (r["sig"] / r["events"] * 100) if r["events"] else 0
        print(f"  {(r['name'] or '?')[:52]:<52} "
              f"{r['events']:>4}ev {r['sig']:>3}sig ({rate:4.1f}%) "
              f"edge={r['avg_edge'] if r['avg_edge'] is not None else '-'}")

    print("\n=== RECENT REJECTIONS ===")
    cols = {r[1] for r in c.execute("PRAGMA table_info(hypotheses)")}
    reason_col = ("rejection_reason" if "rejection_reason" in cols
                  else "notes" if "notes" in cols else None)
    if reason_col:
        rows = list(c.execute(f"""
            SELECT name, sport, {reason_col} AS reason, updated_at
            FROM hypotheses WHERE status='rejected'
            ORDER BY updated_at DESC LIMIT 8"""))
        for r in rows:
            print(f"  {(r['name'] or '?')[:48]:<48} "
                  f"{(r['reason'] or '-')[:60]}")

    print("\n=== SIGNAL EVENTS ===")
    row = c.execute(
        "SELECT COUNT(*), SUM(CASE WHEN signal_generated=1 THEN 1 ELSE 0 END)"
        " FROM backtest_events").fetchone()
    total, sigs = row[0], row[1] or 0
    print(f"  events={total} signals={sigs}"
          + (f" rate={sigs/total*100:.1f}%" if total else ""))
    conn.close()
    return 0


_cmd_status = cmd_status  # backwards-compatible alias
