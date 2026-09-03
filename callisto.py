#!/usr/bin/env python3
"""callisto — the front door.

One CLI for the things a person sitting at this machine actually does:

  ask       one question through the full AGP pipeline; sealed or refused,
            with its confidence, evidence and objections printed. Every run
            is persisted as a JSON record (conclusion, artifact hashes,
            fetch provenance) under the state dir.
  runs      list saved runs, newest first
  show      re-print one run — conclusion, artifacts RE-HASHED against the
            artifact store, fetch provenance
  status    hypothesis-pool / lifecycle counts from the local database
  doctor    can this box run a live question right now? (providers, sources)

Why this exists: before it, driving one real question meant reading
scripts/oxa_live_check.py to learn an argv convention, or hand-wiring
ProviderRouter + RouterModel + ResearchPipeline. The nine root-level
one-off SQL debug scripts (callisto_query*.py, query_*.py, run_query.py,
analysis.py, check_nba_events.py) are quarantined to attic/ — their
overlapping reports are covered by `callisto status`.

Examples:
    python callisto.py ask "Did the 2009 federal minimum wage increase to $7.25?"
    python callisto.py ask --backend gpu1 "Is Bitcoin a good buy right now?"
    python callisto.py status
    python callisto.py doctor
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent

# Command implementations live in tools.cli; callisto.py stays the entry
# script (argparse + main) and re-exports the command functions so existing
# callers/tests that import them from callisto keep working.
from tools.cli.ask import (  # noqa: E402
    _cmd_ask,
    _default_providers_path,
    _load_router,
    _make_engine,
    _persist_run,
    _result_record,
    _runs_dir,
    check_seal_key,
)
from tools.cli.doctor import _cmd_doctor  # noqa: E402
from tools.cli.help import _cmd_help  # noqa: E402
from tools.cli.runs import (  # noqa: E402,F401
    _cmd_runs,
    _cmd_show,
    _fetch_digest_status,
    _load_run,
    _verify_artifact,
)
from tools.cli.status import _cmd_status, _default_db_path as _db_path  # noqa: E402,F401

# ── parser ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="callisto",
        description="Callisto front door — ask, status, doctor.",
        epilog="Money safety defaults: live execution is OFF unless "
               "CALLISTO_ALLOW_LIVE_EXECUTE=1 is set, hosted inference is "
               "stripped when CALLISTO_LOCAL_ONLY=1, and the API binds "
               "to loopback (127.0.0.1) unless CALLISTO_BIND_HOST is set. "
               "Run `callisto doctor` to check this machine.")
    sub = ap.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="ask one research question")
    p_ask.add_argument("question")
    p_ask.add_argument("--backend", help="route all task classes to this "
                                         "provider tier (e.g. gpu1; hosted "
                                         "names refused under "
                                         "CALLISTO_LOCAL_ONLY=1)")
    p_ask.add_argument("--self-review", action="store_true",
                       help="let the adversary run on the same model as the "
                            "author (visible, capped) instead of a separate "
                            "adversary router")
    p_ask.add_argument("--providers", default=_default_providers_path(),
                       help="path to providers.yaml")

    p_status = sub.add_parser(
        "status", help="hypothesis pool / lifecycle summary from the local DB")
    p_runs = sub.add_parser(
        "runs", help="list saved ask() runs (newest first)")
    p_runs.add_argument("--limit", type=int, default=20)
    p_show = sub.add_parser(
        "show", help="show one run's conclusion, artifacts and provenance; "
                     "re-verifies artifact hashes against the store")
    p_show.add_argument("run_id", help="run id (or unique prefix) from `runs`")
    p_doc = sub.add_parser(
        "doctor", help="can this machine answer a live question today?")
    for p in (p_status, p_doc):
        p.add_argument("--providers", default=_default_providers_path())
    sub.add_parser(
        "help", help="show this usage message")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ask":
        return asyncio.run(_cmd_ask(args))
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "runs":
        return _cmd_runs(args)
    if args.command == "show":
        return _cmd_show(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "help":
        return _cmd_help(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
