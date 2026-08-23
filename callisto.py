#!/usr/bin/env python3
"""callisto — the front door.

One CLI for the things a person sitting at this machine actually does:

  ask       one question through the full AGP pipeline; sealed or refused,
            with its confidence, evidence and objections printed
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
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _default_providers_path() -> str:
    return str(REPO / "config" / "providers.yaml")


# ── ask ────────────────────────────────────────────────────────────────────

def _load_router(config_path: str):
    """Seam for tests; production returns a real ProviderRouter."""
    from inference import ProviderRouter
    return ProviderRouter(config_path=config_path)


def _make_engine(router, self_review: bool):
    """Seam for tests; production wires RouterModel + ResearchPipeline."""
    from tools.pipeline.engine import ResearchPipeline
    from tools.pipeline.model import RouterModel
    return ResearchPipeline(
        model=RouterModel(router),
        adversary_router=(None if self_review else router))


async def _cmd_ask(args: argparse.Namespace) -> int:
    router = _load_router(args.providers)
    if args.backend:
        if args.backend not in router.endpoints:
            print(f"unknown provider tier '{args.backend}'. configured: "
                  f"{', '.join(router.endpoints) or '(none)'}")
            return 2
        # Route every task class at the requested endpoint.
        router.task_classes = {tc: args.backend
                               for tc in (router.task_classes or {})}
        router.default_tier_name = args.backend
    try:
        health = await router.check_health(router.default_tier_name)
    except Exception as exc:                       # pragma: no cover
        print(f"provider '{router.default_tier_name}' unreachable: {exc}")
        print("run `python callisto.py doctor` to see what is configured")
        return 2
    if health.get("status") != "ok":
        print(f"provider '{router.default_tier_name}' unhealthy: "
              f"{json.dumps(health)[:300]}")
        return 2

    engine = _make_engine(router, self_review=args.self_review)
    result = await engine.run(args.question)
    print("=" * 72)
    if result.sealed:
        print(f"SEALED   confidence {result.confidence_score:.2f} "
              f"tier={result.confidence_tier}")
    else:
        print("REFUSED")
        if result.refusal_reason:
            print(f"reason   : {result.refusal_reason}")
    for leaf in result.leaves:
        ans = (leaf.answer or "").replace("\n", " ")
        print(f"leaf [{leaf.tier} {leaf.confidence:.2f}] "
              f"{leaf.text[:90]}")
        if ans:
            print(f"     {ans[:400]}")
    srcs = sorted({f.source_name for f in result.fetches})
    print(f"sources  : {len(srcs)} distinct ({', '.join(srcs) or 'none'})"
          f" / {len(result.fetches)} fetches")
    if result.objections:
        print(f"objections ({len(result.objections)}):")
        for ob in result.objections[:5]:
            text = str(getattr(ob, "text", ob))[:220].replace("\n", " ")
            print(f"  - {text}")
    if result.notes:
        print(f"notes    : {'; '.join(result.notes)[:300]}")
    snap = router.cost_ledger.snapshot()
    print(f"cost     : {json.dumps(snap.get('by_tier', {}))}")
    return 0 if result.sealed else 1


# ── status ────────────────────────────────────────────────────────────────

def _db_path() -> str:
    return os.getenv("CALLISTO_DB_PATH",
                     str(REPO / "memory" / "callisto.db"))


def _cmd_status(args: argparse.Namespace) -> int:
    import sqlite3

    db = _db_path()
    if not Path(db).exists():
        print(f"no database at {db} — nothing has run on this machine yet")
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


# ── doctor ────────────────────────────────────────────────────────────────

def _cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    provs: dict = {}
    print("== providers ==")
    try:
        from inference import load_providers_config
        cfg = load_providers_config(args.providers)
        default = cfg.get("default_tier") if isinstance(cfg, dict) else None
        provs = cfg.get("providers", {}) if isinstance(cfg, dict) else {}
        for name, ep in provs.items():
            mark = "*" if name == default else " "
            print(f"  {mark}{name:<12} backend={ep.get('backend')}"
                  f" model={ep.get('model','-')}"
                  f" concurrency={ep.get('max_concurrency','?')}")
        if not provs:
            print("  NO PROVIDERS CONFIGURED"); ok = False
    except Exception as exc:
        print(f"  config unreadable: {exc}"); ok = False

    print("== hermes cli ==")
    try:
        from tools.pipeline.hermes_cli import hermes_available
        avail = hermes_available()
    except Exception as exc:
        avail = False
        print(f"  check failed: {exc}")
    print(f"  available: {avail}")
    needs_hermes = any(p.get("backend") == "hermes_cli"
                       for p in provs.values())
    if needs_hermes and not avail:
        print("  a configured provider uses backend=hermes_cli but the CLI")
        print("  is not reachable — those tiers will fail at ask time")
        ok = False

    print("== database ==")
    db = _db_path()
    print(f"  path: {db}")
    print(f"  present: {Path(db).exists()}")

    print("== source registry ==")
    try:
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()
        names = sorted(reg.names())
        print(f"  {len(names)} adapters registered: {', '.join(names)}")
        if not names:
            ok = False
    except Exception as exc:
        print(f"  registry unavailable: {exc}"); ok = False

    print("\ndoctor:", "OK" if ok else "PROBLEMS FOUND (see above)")
    return 0 if ok else 1


# ── parser ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="callisto",
        description="Callisto front door — ask, status, doctor.")
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
    p_doc = sub.add_parser(
        "doctor", help="can this machine answer a live question today?")
    for p in (p_status, p_doc):
        p.add_argument("--providers", default=_default_providers_path())
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ask":
        return asyncio.run(_cmd_ask(args))
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
