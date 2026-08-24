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
  predict   register a recurring claim + commit a probability BEFORE ground
            truth (preregistered forward-testing; the lifecycle's intake)
  predictions
            unresolved predictions awaiting their deadline — what to grade
  resolve   record ground truth for one prediction; prints the claim's
            track record and the confidence ceiling it has now earned
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
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _runs_dir() -> Path:
    """Directory where ask() results are persisted as JSON records.

    Uses the off-OneDrive state dir (tools.state_paths) so a run record
    never freezes under a sync lock; overridable with CALLISTO_RUNS_DIR.
    """
    override = os.getenv("CALLISTO_RUNS_DIR", "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        try:
            from tools.state_paths import state_dir
            root = state_dir() / "runs"
        except Exception:
            root = Path.home() / ".local" / "state" / "callisto" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


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


def _result_record(result, question: str) -> dict:
    """Serialise a PipelineResult into the persisted run record.

    Everything needed to re-check the conclusion later: the conclusion
    text itself, every artifact hash (resolvable against the artifact
    store), and per-fetch source/URL provenance.
    """
    return {
        "recorded_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "question": question,
        "sealed": bool(getattr(result, "sealed", False)),
        "refusal_reason": getattr(result, "refusal_reason", ""),
        "conclusion": getattr(result, "conclusion", ""),
        "confidence": {
            "score": getattr(result, "confidence_score", 0.0),
            "tier": getattr(result, "confidence_tier", "UNVERIFIED"),
        },
        "leaves": [
            {"text": lf.text, "answer": lf.answer or "",
             "tier": lf.tier, "confidence": lf.confidence}
            for lf in getattr(result, "leaves", [])
        ],
        "artifacts": [r.to_dict() for r in getattr(result, "artifact_refs", [])],
        "fetches": [
            {"source": getattr(f, "source_name", "?"),
             "url": getattr(f, "url", ""),
             "content_sha256": getattr(f, "content_sha256", "")}
            for f in getattr(result, "fetches", [])
        ],
        "objections": [getattr(o, "text", str(o))
                       for o in getattr(result, "objections", [])],
        "notes": list(getattr(result, "notes", [])),
    }


def _persist_run(record: dict) -> Path:
    """Write the run record atomically; returns its path. The filename is
    the timestamped run id — `callisto runs` / `callisto show` read it."""
    stamp = record["recorded_at"].replace(":", "").replace("-", "")
    run_id = f"{stamp}_{abs(hash(record['question'])) % 10000:04d}"
    path = _runs_dir() / f"{run_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


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

    # Persist the full record — conclusion, artifact hashes, fetch
    # provenance — so a human can re-check it after the terminal scrolls.
    try:
        record = _result_record(result, args.question)
        path = _persist_run(record)
        print(f"run      : {path}")
        for ref in getattr(result, "artifact_refs", []):
            print(f"artifact : {ref.kind:<5} {ref.sha256[:16]}…  {ref.name}")
    except Exception as exc:
        print(f"run      : NOT SAVED ({exc})")
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
    if fetches:
        print(f"\n--- fetches ({len(fetches)}) ---")
        seen = set()
        for f in fetches:
            key = (f.get("source", "?"), f.get("url", ""))
            if key in seen:
                continue
            seen.add(key)
            print(f"  {key[0]:<18} {key[1][:90]}")
    obs = rec.get("objections", [])
    if obs:
        print(f"\nobjections ({len(obs)}):")
        for o in obs[:5]:
            print(f"  - {str(o)[:200]}")
    print(f"\nrecord   : {path}")
    return 0


# ── predict / predictions / resolve ───────────────────────────────────────

async def _open_journal():
    """Ensure the lifecycle schema exists locally, then return
    (PredictionJournal, db). The DB is created on first use."""
    from tools.schema.engine import ensure_schema, open_db
    from tools.resolvers.generic import PredictionJournal
    path = _db_path()
    await ensure_schema(path)
    db = await open_db(path)
    return PredictionJournal(db), db


def _cmd_predict(args: argparse.Namespace) -> int:
    if not (0.0 <= args.prob <= 1.0):
        print(f"--prob must be within [0,1], got {args.prob}")
        return 2
    if not args.by:
        print("--by is required: a prediction without a deadline can never "
              "be scored stale, so it cannot earn confidence")
        return 2

    async def run():
        journal, db = await _open_journal()
        try:
            cid = await journal.create_claim(
                name=args.claim,
                thesis=f"{args.event} (deadline {args.by})",
                notes="registered via callisto predict")
            pid = await journal.record_prediction(
                claim_id=cid, event_id=args.event, predicted_prob=args.prob,
                due_at=args.by)
            cur = await db.execute(
                "SELECT status FROM hypotheses WHERE hypothesis_id = ?", (cid,))
            row = await cur.fetchone()
            print(f"claim      : {args.claim} ({cid})")
            print(f"prediction : #{pid}  p={args.prob:.2f}  due {args.by}")
            print(f"stage      : {row[0] if row else '?'} "
                  "(preregistered forward-testing)")
            print("next       : when ground truth arrives, "
                  "`callisto resolve`")
        finally:
            await db.close()
    return asyncio.run(run())


def _cmd_predictions(args: argparse.Namespace) -> int:
    async def run():
        journal, db = await _open_journal()
        try:
            rows = await journal.open_predictions(
                None if not args.claim else args.claim)
            if not rows:
                print("no open predictions — `callisto predict` registers one")
                return 0
            today = datetime.date.today().isoformat()
            for r in rows:
                due = r.get("due_at") or "no deadline"
                mark = ""
                if r.get("due_at") and str(r["due_at"])[:10] < today:
                    mark = "  OVERDUE (scores stale until resolved)"
                print(f"#{r['id']:<4} p={r['predicted_prob']:.2f} "
                      f"due {due}  [{r.get('claim_name') or r['claim_id']}]"
                      f"{mark}")
                print(f"     {r['event_id'][:100]}")
        finally:
            await db.close()
    return asyncio.run(run())


def _cmd_resolve(args: argparse.Namespace) -> int:
    async def run():
        journal, db = await _open_journal()
        try:
            cur = await db.execute(
                "SELECT claim_id FROM predictions WHERE id = ?",
                (args.prediction_id,))
            row = await cur.fetchone()
            if not row:
                print(f"no prediction #{args.prediction_id} — see "
                      "`callisto predictions`")
                return 1
            claim_id = row[0]
            res = await journal.resolve_prediction(
                args.prediction_id, args.outcome, payoff=args.payoff)
            tag = "(already recorded)" if res["idempotent"] else "recorded"
            print(f"prediction #{args.prediction_id}: "
                  f"{res['resolved_outcome']} {tag}")
            s = await journal.track_summary(claim_id)
            brier = "-" if s["brier"] is None else f"{s['brier']:.3f}"
            print(f"track record: n={s['n_resolved']} hits={s['n_hit']} "
                  f"stale={s['n_stale']} brier={brier}")
            print(f"inherited ceiling for '{claim_id}': "
                  f"{s['inherited_ceiling']:.2f} ({s['ceiling_tier']}) "
                  f"— earned from resolved predictions only")
        except Exception as exc:
            print(f"refused: {exc}")
            return 2
        finally:
            await db.close()
    return asyncio.run(run())


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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
