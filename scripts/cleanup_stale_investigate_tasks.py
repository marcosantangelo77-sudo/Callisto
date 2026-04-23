"""One-time cleanup: collapse duplicate SLA-watchdog investigate-tasks.

The SLA watchdog in ``api.py::ingestion_sla_watchdog_loop`` filed one
"investigate: ingestion source 'X' has not successfully ingested …" task
per source per restart, and watchdog-driven restarts accumulated 599
PENDING duplicates in the queue on 2026-04-23.

The underlying re-fire bug is fixed in ``feat/sla-watchdog-persist``
(persistent alerted-source state + task-queue-level dedup). This script
cleans up the backlog already sitting in ``task_queue``:

  * For every source with ≥ 1 PENDING investigate-task, keep the OLDEST
    (lowest ``created_at``) row and mark every other PENDING duplicate
    for that source as ``FAILED`` with an explanatory error message
    (``task_queue`` has no ``CANCELLED`` bucket in its CHECK constraint).
  * PROCESSING / COMPLETED / FAILED rows are left untouched.
  * A single source with one PENDING row is a no-op.

Usage:
    # Dry-run — prints the verdict table, no mutation:
    python scripts/cleanup_stale_investigate_tasks.py

    # Apply (requires BOTH flags):
    python scripts/cleanup_stale_investigate_tasks.py --live --yes

    # Also collapse PROCESSING (not recommended — may cancel a running
    # session):
    python scripts/cleanup_stale_investigate_tasks.py --include-processing

Exit codes:
    0  clean run (dry-run or live)
    2  --live without --yes
    4  DB connect failure

IMPORTANT: run this ONCE after merging feat/sla-watchdog-persist. The
running API must be restarted before running this script so the new
dedup layer is active — otherwise a fresh SLA tick could immediately
re-file the same duplicates.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger("callisto.cleanup_investigate")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

INVESTIGATE_PREFIX = "investigate: ingestion source"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Collapse duplicate SLA-watchdog investigate-tasks.",
    )
    p.add_argument(
        "--db",
        type=str,
        default=os.getenv(
            "CALLISTO_DB_PATH",
            str(_REPO_ROOT / "memory" / "callisto.db"),
        ),
        help="Path to callisto.db (default: $CALLISTO_DB_PATH or ./memory/callisto.db).",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Actually mutate the DB (default: dry-run).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Required alongside --live to proceed.",
    )
    p.add_argument(
        "--include-processing",
        action="store_true",
        help=(
            "Also collapse PROCESSING rows. OFF by default because a "
            "PROCESSING row means the worker is running that session now."
        ),
    )
    return p


def extract_source(query: str) -> str | None:
    """Parse the source name out of an investigate query string.

    Query format (from api.py::ingestion_sla_watchdog_loop):
        investigate: ingestion source 'SOURCE_NAME' has not …
    """
    prefix = f"{INVESTIGATE_PREFIX} '"
    if not query.startswith(prefix):
        return None
    rest = query[len(prefix):]
    end = rest.find("'")
    if end < 0:
        return None
    return rest[:end] or None


def main() -> int:
    args = build_parser().parse_args()

    if args.live and not args.yes:
        print(
            "ERROR: --live requires --yes (explicit confirmation).\n"
            "       This is a DB mutation; dry-run first.",
            file=sys.stderr,
        )
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 4

    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout = 30000")
    except sqlite3.Error as e:
        print(f"ERROR: DB connect failed: {e}", file=sys.stderr)
        return 4

    try:
        statuses = ("PENDING", "PROCESSING") if args.include_processing else ("PENDING",)
        placeholders = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT task_id, query, status, created_at "
            f"FROM task_queue "
            f"WHERE status IN ({placeholders}) "
            f"  AND query LIKE ? "
            f"ORDER BY created_at ASC, task_id ASC",
            (*statuses, f"{INVESTIGATE_PREFIX}%"),
        ).fetchall()

        # Bucket by source
        by_source: dict[str, list[tuple[int, str, str, str]]] = {}
        unparsable: list[tuple[int, str, str, str]] = []
        for task_id, query, status, created_at in rows:
            src = extract_source(query)
            if src is None:
                unparsable.append((task_id, query, status, created_at))
                continue
            by_source.setdefault(src, []).append((task_id, query, status, created_at))

        total_rows = len(rows)
        sources = sorted(by_source.keys())

        print(
            f"\nFound {total_rows} matching rows across {len(sources)} sources "
            f"(status filter: {','.join(statuses)})."
        )
        if unparsable:
            print(
                f"  {len(unparsable)} row(s) didn't match the expected "
                f"query format and will be left alone."
            )

        to_cancel: list[int] = []
        keep_ids: list[int] = []
        for src in sources:
            bucket = by_source[src]
            bucket.sort(key=lambda r: (r[3] or "", r[0]))  # oldest first
            keeper = bucket[0]
            dupes = bucket[1:]
            keep_ids.append(keeper[0])
            to_cancel.extend(d[0] for d in dupes)
            print(
                f"  {src:40s}  keep task_id={keeper[0]} ({keeper[3]}), "
                f"cancel {len(dupes)}"
            )

        print(
            f"\nWould keep {len(keep_ids)}, cancel {len(to_cancel)} "
            f"(unparsable left alone: {len(unparsable)})."
        )

        if not args.live:
            print("\nDRY-RUN — no DB mutation performed. Pass --live --yes to apply.")
            return 0

        if not to_cancel:
            print("\nNothing to cancel — queue already clean.")
            return 0

        # task_queue.CHECK(status IN (PENDING, PROCESSING, COMPLETED,
        # FAILED, TIMEOUT)) — no CANCELLED bucket, so mark as FAILED with
        # an explanatory error.
        target_status = "FAILED"
        error_msg = (
            "Superseded by SLA-watchdog dedup "
            "(feat/sla-watchdog-persist cleanup)"
        )
        logger.info(f"Cancelling {len(to_cancel)} row(s) with status={target_status}")
        conn.executemany(
            f"UPDATE task_queue "
            f"SET status = ?, "
            f"    completed_at = datetime('now'), "
            f"    error = ? "
            f"WHERE task_id = ? AND status IN ({placeholders})",
            [(target_status, error_msg, tid, *statuses) for tid in to_cancel],
        )
        conn.commit()

        remaining = conn.execute(
            f"SELECT COUNT(*) FROM task_queue "
            f"WHERE status IN ({placeholders}) "
            f"  AND query LIKE ?",
            (*statuses, f"{INVESTIGATE_PREFIX}%"),
        ).fetchone()[0]
        print(
            f"\nCleanup complete. Remaining matching rows "
            f"({','.join(statuses)}): {remaining}"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
