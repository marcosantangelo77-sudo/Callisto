#!/usr/bin/env python
"""Standalone migration CLI.

Usage:
    python scripts/migrate.py                 # status (applied/pending/version)
    python scripts/migrate.py --status        # alias for the default
    python scripts/migrate.py --dry-run       # list pending versions + show
                                              # the SQL each would run (best-
                                              # effort — Python up() functions
                                              # can't be introspected
                                              # losslessly, so this shows the
                                              # module source instead).
    python scripts/migrate.py --apply         # actually apply pending migrations
    python scripts/migrate.py --db PATH ...   # override CALLISTO_DB_PATH

Exit codes:
    0   success / nothing to do
    1   migration failed during --apply
    2   invalid arguments
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.migrations import (
    apply_pending_migrations,
    discover_migrations,
    get_migration_status,
)


def _default_db_path() -> str:
    return os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


def cmd_status(db_path: str) -> int:
    status = get_migration_status(db_path)
    print(json.dumps(status, indent=2))
    return 0


def cmd_dry_run(db_path: str) -> int:
    status = get_migration_status(db_path)
    print(f"schema_version: {status['schema_version']}")
    print(f"applied_count:  {len(status['applied'])}")
    print(f"pending_count:  {len(status['pending'])}")
    if status["drift"]:
        print("\n*** CHECKSUM DRIFT DETECTED (migration edited after apply) ***")
        for d in status["drift"]:
            print(f"  {d['version']:03d}_{d['name']}: "
                  f"stored={d['stored_checksum'][:12]}... "
                  f"current={d['current_checksum'][:12]}...")
    if not status["pending"]:
        print("\nNo pending migrations.")
        return 0
    print("\nPending migrations (would be applied, in order):")
    migrations_by_version = {m.version: m for m in discover_migrations()}
    for p in status["pending"]:
        mig = migrations_by_version.get(p["version"])
        if mig is None:
            print(f"  {p['version']:03d}_{p['name']}  (module not found?)")
            continue
        up_mod = inspect.getmodule(mig.up)
        src_file = getattr(up_mod, "__file__", "<unknown>")
        print(f"\n--- {p['version']:03d}_{p['name']}  ({src_file}) ---")
        try:
            src = inspect.getsource(mig.up)
        except OSError:
            src = "<source unavailable>"
        for line in src.splitlines():
            print(f"    {line}")
    print("\nRun with --apply to execute.")
    return 0


def cmd_apply(db_path: str) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        result = apply_pending_migrations(db_path)
    except Exception as e:  # pragma: no cover - surfaced to CLI
        print(f"MIGRATION FAILED: {e!r}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate",
        description="Callisto migration CLI (status / dry-run / apply).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite DB (default: $CALLISTO_DB_PATH or "
             "memory/callisto.db).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--status", action="store_true",
        help="Show applied/pending migrations (default if no mode given).",
    )
    mode.add_argument(
        "--dry-run", action="store_true",
        help="Show pending migrations and the source of each up() without "
             "applying anything.",
    )
    mode.add_argument(
        "--apply", action="store_true",
        help="Apply pending migrations (writes to the DB).",
    )

    args = parser.parse_args(argv)
    db_path = args.db or _default_db_path()

    if not os.path.exists(db_path) and not args.apply:
        # status / dry-run on a non-existent DB is fine — discover_migrations
        # still works without a live file. The runner auto-creates the parent
        # dir on --apply.
        print(f"warning: {db_path} does not exist", file=sys.stderr)

    if args.apply:
        return cmd_apply(db_path)
    if args.dry_run:
        return cmd_dry_run(db_path)
    return cmd_status(db_path)


if __name__ == "__main__":
    sys.exit(main())
