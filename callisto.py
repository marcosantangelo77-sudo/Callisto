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
from tools.cli.status import _cmd_status, _default_db_path as _db_path  # noqa: E402,F401


# ── runs / show ───────────────────────────────────────────────────────────

def _load_run(run_id: str) -> tuple[Optional[dict], Optional[Path]]:
    """Load a run record by id (filename stem) or unique prefix."""
    runs = sorted(_runs_dir().glob(f"{run_id}*.json"))
    if not runs:
        return None, None
    if len(runs) > 1:
        raise SystemExit(
            f"ambiguous run id '{run_id}' matches {len(runs)} records; "
            "use a longer prefix")
    return json.loads(runs[0].read_text(encoding="utf-8")), runs[0]


def _verify_artifact(sha256: str) -> str:
    """Re-hash the artifact against its recorded hash. Returns a status."""
    try:
        from tools.artifacts import ArtifactStore, sha256_bytes
        store = ArtifactStore()
        actual = sha256_bytes(store.get_bytes(sha256))
        return "ok" if actual == sha256 else "CORRUPT"
    except Exception as exc:
        short = str(exc)
        return "missing" if "not found" in short else f"unverifiable: {short}"


def _cmd_runs(args: argparse.Namespace) -> int:
    paths = sorted(_runs_dir().glob("*.json"), reverse=True)[:args.limit]
    if not paths:
        print("no saved runs yet — `callisto ask \"...\"` creates one")
        return 0
    for p in paths:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            verdict = ("SEALED" if rec.get("sealed") else "REFUSED")
            conf = rec.get("confidence", {})
            q = (rec.get("question") or "?")[:60]
            print(f"{p.stem}  {verdict:<8} "
                  f"{conf.get('tier', '?')}/{conf.get('score', 0):.2f}  {q}")
        except Exception as exc:
            print(f"{p.stem}  (unreadable: {exc})")
    return 0


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _fetch_digest_status(f: dict) -> tuple[str, bool]:
    """Validate one persisted fetch's content_sha256.

    Returns (status, hard_fail). "ok" means verified; hard_fail marks a
    missing/non-string/wrong-length/non-hex digest — absence is failure
    (red-team C1/D3), and it makes `show` exit non-zero. A syntactically
    valid digest with no local payload cannot be checked against bytes here
    (no network fetch), so it is flagged unverified but keeps legacy
    compatibility (soft).
    """
    digest = f.get("content_sha256")
    if not isinstance(digest, str) or not digest:
        return "MISSING DIGEST", True
    d = digest.strip().lower()
    if len(d) != 64:
        return f"MALFORMED DIGEST ({len(d)} chars)", True
    if not _HEX64_RE.match(d):
        return "MALFORMED DIGEST (non-hex)", True
    body = None
    for k in ("body", "content", "payload"):
        v = f.get(k)
        if isinstance(v, str):
            body = v.encode("utf-8")
            break
        if isinstance(v, (bytes, bytearray)):
            body = bytes(v)
            break
    if body is None:
        # No local payload to hash — remote content is not fetched here, so
        # the recorded digest cannot be verified, only syntax-checked.
        return "unverified (no local payload)", False
    if hashlib.sha256(body).hexdigest() != d:
        return "DIGEST MISMATCH", True
    return "ok", False


def _cmd_show(args: argparse.Namespace) -> int:
    rec, path = _load_run(args.run_id)
    if rec is None:
        print(f"no run matching '{args.run_id}' — see `callisto runs`")
        return 1
    verdict = "SEALED" if rec.get("sealed") else "REFUSED"
    conf = rec.get("confidence", {})
    print(f"run      : {path.stem}")
    print(f"when     : {rec.get('recorded_at', '?')}")
    print(f"question : {rec.get('question', '?')}")
    print(f"{verdict:<9}: {conf.get('tier', '?')} {conf.get('score', 0):.2f}")
    if rec.get("refusal_reason"):
        print(f"reason   : {rec['refusal_reason']}")
    if rec.get("conclusion"):
        print("\n--- conclusion ---")
        print(rec["conclusion"])
    arts = rec.get("artifacts", [])
    if arts:
        print(f"\n--- artifacts ({len(arts)}) — re-hashed against the store ---")
        for a in arts:
            status = _verify_artifact(a["sha256"])
            print(f"  [{status:<12}] {a['kind']:<5} "
                  f"{a['sha256'][:16]}…  {a.get('name', '')}")
    fetches = rec.get("fetches", [])
    bad_fetches = 0
    if fetches:
        print(f"\n--- fetches ({len(fetches)}) — provenance digests checked ---")
        seen = set()
        # Validate EVERY persisted record first — deduplication must never
        # hide an invalid sibling behind an earlier valid (source, url).
        results = [(f, *_fetch_digest_status(f)) for f in fetches]
        for f, status, hard_fail in results:
            key = (f.get("source", "?"), f.get("url", ""))
            if key in seen and status == "ok":
                continue
            seen.add(key)
            if status != "ok":
                if hard_fail:
                    bad_fetches += 1
                print(f"  [{status:<22}] {key[0]:<18} {key[1][:70]}")
            else:
                print(f"  [ok]                  {key[0]:<18} {key[1][:70]}")
        if bad_fetches:
            print(f"  WARNING: {bad_fetches} fetch(es) have missing or "
                  "malformed content_sha256 provenance — UNVERIFIED.")
    obs = rec.get("objections", [])
    if obs:
        print(f"\nobjections ({len(obs)}):")
        for o in obs[:5]:
            print(f"  - {str(o)[:200]}")
    print(f"\nrecord   : {path}")
    return 1 if bad_fetches else 0


# ── parser ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="callisto",
        description="Callisto front door — ask, status, doctor.",
        epilog="Money safety defaults: live execution is OFF unless "
               "CALLISTO_ALLOW_LIVE_EXECUTE=1 is set, and the API binds "
               "to loopback (127.0.0.1) unless CALLISTO_BIND_HOST is set. "
               "Run `callisto doctor` to check this machine.")
    sub = ap.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="ask one research question")
    p_ask.add_argument("question")
    p_ask.add_argument("--backend", help="route all task classes to this "
                                         "provider tier (e.g. ox_alpha, gpu1)")
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
