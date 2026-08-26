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
    # First-run durability caveat (documented, deliberate): when this call
    # CREATES the runs directory, the parent-directory entry for the new
    # dir itself is not fsynced here — many filesystems reject directory
    # fsync of a not-yet-durable parent, and adding a best-effort parent
    # fsync would shift the fsync sequencing contract relied upon by
    # persistence tests and the reservation/tmp fsync ordering. Until some
    # later successful directory fsync (every publication performs one),
    # crash durability of the freshly created runs dir itself is
    # unconfirmed. Every *record* published inside it still gets full
    # file+directory fsync treatment in _persist_run.
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
    the timestamped run id — `callisto runs` / `callisto show` read it.

    Publication sequence, split into an explicit PRE-publication phase and
    a POST-publication phase (the boundary is the os.replace()):

      PRE (all failures are retryable and must leave no sidecars):
        1. Reserve the id by O_EXCL-creating `<id>.json.resv` — a private
           reservation marker that no `*.json` glob ever matches.
        2. Revalidate final absence UNDER the reservation: another writer
           may have published this exact id and released its reservation
           between our cheap existence pre-check and our O_EXCL win.
        3. Write the complete payload to a `.tmp` sibling, flush() it, and
           fsync the tmp file. Any write/flush/fsync failure here is a
           pre-commit failure: cleanup removes both the tmp and the resv,
           and the same sequence slot may be safely retried.

      COMMIT: os.replace(tmp, final) — atomic swap; no reader ever sees a
        partial or empty final JSON. This call is the point of no return.

      POST (failures here are NEVER retried as new records):
        4. Re-verify the final still holds this call's payload — an
           external writer may have displaced it between the rename and
           verification. A displaced/foreign final is reported as
           `PublicationIndeterminate`, never a normal success.
        5. fsync the published file, then the directory, for durability.
           If either durability confirmation fails, we deliberately raise
           `DurabilityError` naming the already-published path instead of
           retrying: retrying would observe `_000.json` visible on disk and
           publish the SAME logical run again as `_001.json`. The record is
           published and readable; only its crash-durability guarantee is
           in doubt, and that is reported honestly rather than papered over
           by a duplicate. The existing final file is never overwritten.

    Cleanup of the reservation fd/marker and any tmp sibling runs in
    `finally` blocks scoped so that even a failing close()/unlink() cannot
    skip other required cleanup or mask the real outcome. Only a hard
    process crash can leave a stale `.resv`; never malformed `*.json`.
    """
    stamp = record["recorded_at"].replace(":", "").replace("-", "")
    qhash = hashlib.sha256(
        str(record.get("question", "")).encode("utf-8")).hexdigest()[:8]
    base = _runs_dir()
    payload = json.dumps(record, indent=2).encode("utf-8")
    attempts_left = 1000
    seq = 0
    while attempts_left:
        attempts_left -= 1
        run_id = f"{stamp}_{qhash}_{seq:03d}"
        path = base / f"{run_id}.json"
        resv = base / f"{run_id}.json.resv"
        tmp = path.with_suffix(".json.tmp")
        # Fast path: skip slots whose record is already published. The
        # authoritative recheck happens under the reservation below.
        if path.exists():
            seq += 1
            continue
        # Atomic private claim: fails with FileExistsError if another writer
        # holds this exact id (same second, same question, same seq).
        try:
            fd = os.open(resv, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            seq += 1          # slot genuinely taken; move on
            continue

        # Outcome protocol for this attempt. The try/finally performs
        # cleanup ONLY; every control decision is deferred until after the
        # finally block so a failing cleanup can never mask or replace the
        # real outcome silently.
        action = None          # None | "retry" | "next" | "done" | "fail"
        result_path = None
        error = None
        published_path = None
        cleanup_errors = []
        try:
            # 1. Durable reservation. Failure is pre-publication: after
            #    cleanup the SAME sequence slot may be reused.
            try:
                os.write(fd, b"")
                os.fsync(fd)
            except OSError:
                action = "retry"
            if action is None:
                # Release the reservation descriptor before publishing;
                # unlinking the marker happens in the finally below.
                try:
                    os.close(fd)
                except OSError:
                    pass                    # best-effort close
                fd = -1                     # mark closed
                # 2. Revalidate final absence UNDER the reservation: writer
                #    A may have published this exact id and released its
                #    reservation between our cheap exists() check and our
                #    O_EXCL win. Replacing over A's record would silently
                #    destroy it — move to the next slot instead.
                if path.exists():
                    action = "next"
            # 3. Pre-publication: build + durably flush the tmp payload.
            if action is None:
                try:
                    with open(tmp, "wb") as fh:
                        written = fh.write(payload)
                        if written != len(payload):
                            # A short write means the tmp file does NOT
                            # hold the intended payload. Treating it as
                            # publishable would put an unparseable/truncated
                            # JSON record behind the atomic rename.
                            raise OSError(
                                f"short write: {written} of "
                                f"{len(payload)} bytes reached the temp file")
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError:
                    # Pre-commit failure: nothing was published, so after
                    # cleanup (finally below) the same slot is reusable.
                    action = "retry"
            # 4. COMMIT: atomic content publication (point of no return).
            #    Capture the tmp inode BEFORE the attempt: on success (or
            #    success-then-error) the tmp name is gone, and inode
            #    identity between final and that captured stat is the only
            #    sound proof this call published.
            tmp_stat = None
            if action is None:
                try:
                    tmp_stat = os.stat(tmp)
                except OSError:
                    tmp_stat = None
                try:
                    os.replace(tmp, path)
                    published_path = path
                except OSError:
                    # os.replace gives no guarantee whether the swap
                    # happened before the error. Only provable provenance
                    # counts as publication: the destination inode being
                    # THIS call's tmp inode (`_final_is_our_tmp`). A present,
                    # byte-identical foreign file proves content, not
                    # provenance, and is classified indeterminate; an
                    # absent destination is a clean retryable failure.
                    # Byte equality alone proves content, not provenance:
                    # an external writer may have placed byte-identical
                    # JSON at the destination. Only the inode of OUR tmp
                    # file appearing at `path` proves THIS call published.
                    if _final_is_our_tmp(path, tmp_stat):
                        published_path = path
                    elif (not path.exists()
                          and _published_payload_matches(path, payload) is False):
                        # Destination provably absent: nothing was published
                        # and no foreign content exists. Pre-commit failure:
                        # cleanup removes the tmp and the slot may be retried.
                        action = "retry"
                    else:
                        detail = (
                            "os.replace failed and the destination could "
                            "not be read for verification"
                            if not path.exists() else
                            "os.replace failed and the destination holds "
                            "different (foreign/malformed) content")
                        error = PublicationIndeterminate(detail, path)
                        action = "fail"
            # 5. POST-publication durability confirmation. Never retried as
            #    a new record from here: `_000.json` is visible, so any
            #    retry loop would republish the same logical run as
            #    `_001.json`.
            if action is None:
                # Post-commit external race: an external writer may have
                # replaced the final between our rename and this check.
                # A final that no longer holds THIS call's payload must
                # never yield a normal success — report indeterminate
                # (the record was published but has since been displaced).
                if not _published_payload_matches(path, payload):
                    error = PublicationIndeterminate(
                        "the published final no longer matches this "
                        "call's intended payload (external race after "
                        "commit)", path)
                    action = "fail"
            if action is None:
                problems = []
                try:
                    ffd = os.open(path, os.O_RDONLY)
                except OSError as exc:
                    problems.append(
                        f"published file could not be reopened: {exc}")
                else:
                    try:
                        os.fsync(ffd)      # durability of published content
                    except OSError as exc:
                        problems.append(f"final-file fsync failed: {exc}")
                    finally:
                        try:
                            os.close(ffd)
                        except OSError:
                            pass
                try:
                    dfd = os.open(base, os.O_RDONLY)
                except OSError as exc:
                    problems.append(
                        "runs directory could not be opened for fsync: "
                        f"{exc}")
                else:
                    try:
                        os.fsync(dfd)      # directory entry durability
                    except OSError as exc:
                        problems.append(f"directory fsync failed: {exc}")
                    finally:
                        try:
                            os.close(dfd)
                        except OSError:
                            pass
                if problems:
                    error = DurabilityError(path, "; ".join(problems))
                    action = "fail"
                else:
                    result_path = path
                    action = "done"
        finally:
            # Cleanup of the reservation descriptor/marker and the tmp
            # sibling. Each step is independent — one failure cannot skip
            # the others — but failures are RECORDED, not swallowed: see
            # the outcome resolution below the finally block.
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                resv.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(f"{resv.name}: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(f"{tmp.name}: {exc}")

        # ── Outcome resolution (deliberate, never silent) ──
        if cleanup_errors:
            residue = "; ".join(cleanup_errors)
            if published_path is not None:
                # The record IS published at `path`; report durability/
                # residue honestly without ever duplicating the record.
                prior = str(error) if error is not None else ""
                detail = (prior + "; " if prior else "") + (
                    "residual sidecar file(s) could not be removed: "
                    + residue)
                raise DurabilityError(published_path, detail)
            # Nothing was published, but cleanup is incomplete: the stale
            # marker poisons this sequence slot. Refuse to claim a clean
            # retryable outcome — surface the residue instead.
            raise PersistenceCleanupError([resv, tmp], residue)
        if action == "done":
            return result_path
        if action == "fail":
            raise error
        if action == "next":
            seq += 1
            continue
        # action == "retry": same slot is safe to reuse after cleanup.
        continue
    raise RuntimeError(
        "could not allocate a unique run id after 1000 attempts "
        f"for stamp {stamp}")


def _final_is_our_tmp(path: Path, tmp_stat: "os.stat_result | None") -> bool:
    """Provenance check: does `path` now reference the SAME inode as the
    `.tmp` file this call created, per `tmp_stat` (captured BEFORE the
    replace attempt)?

    Byte-exact content matching is NOT proof of provenance — an external
    writer can place byte-identical JSON at the destination after a failed
    rename. os.replace() makes the destination name point at the source
    inode, so identity of st_ino/st_dev proves THIS call published. If the
    tmp could not be stat'ed before the attempt, provenance cannot be
    established and the caller must treat publication as indeterminate.
    """
    if tmp_stat is None:
        return False
    try:
        fstat = os.stat(path)
    except OSError:
        return False
    return (fstat.st_ino, fstat.st_dev) == \
        (tmp_stat.st_ino, tmp_stat.st_dev)


def _published_payload_matches(path: Path, payload: bytes) -> bool | None:
    """Does the file at `path` contain exactly `payload`?

    True  — byte-exact match: this call demonstrably published it.
    False — the file is absent or holds different (foreign) content.
    None  — the file exists but cannot be read: publication cannot be
            proven either way.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return data == payload


class DurabilityError(RuntimeError):
    """The run record was successfully published at `path`, but confirming
    its crash durability (file/directory fsync) failed. Callers may treat
    the record as present-but-unconfirmed; `_persist_run` will NOT silently
    republish it under a different sequence number."""

    def __init__(self, path: Path, detail: str):
        super().__init__(
            f"run record published at {path} but durability could not be "
            f"confirmed: {detail}")
        self.path = path


class PublicationIndeterminate(RuntimeError):
    """It could not be determined whether THIS call's payload was published
    at the target path (os.replace raised, and the destination holds
    unreadable or foreign/malformed content). Callers must NOT retry
    blindly: doing so risks either duplicating the run or leaving the
    foreign file in place while assuming success. Inspect the named path
    (and any `.tmp` sibling) manually."""

    def __init__(self, detail: str, path: Path):
        super().__init__(
            f"publication state indeterminate for {path}: {detail}; "
            f"inspect {path} (and any .json.tmp sibling) before retrying")
        self.path = path


class PersistenceCleanupError(RuntimeError):
    """A pre-publication attempt failed AND its sidecar cleanup could not
    complete: nothing was published by this call, but residual `.resv` /
    `.tmp` files remain on disk and poison the affected sequence slot(s).
    This is deliberately NOT reported as a clean retryable failure."""

    def __init__(self, residues: list, detail: str):
        self.residues = list(residues)
        super().__init__(
            f"run record not published; residual sidecar file(s) could not "
            f"be removed: {detail}. Remove them manually before retrying.")


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
        # A pinned backend disables failover, so refuse early if it is
        # unreachable. With no --backend, skip the preflight and let the
        # ProviderRouter candidate chain (which includes the OX fallback)
        # make the routing decision per task.
        try:
            health = await router.check_health(router.default_tier_name)
        except Exception as exc:                   # pragma: no cover
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
    except DurabilityError as exc:
        # The record IS published and readable at `exc.path`; only its
        # crash-durability guarantee is unconfirmed. Reporting this as
        # "NOT SAVED" would invite a retry that creates a DUPLICATE run.
        # Verify `exc.path` exists before re-running anything.
        print(f"run      : SAVED WITH UNCONFIRMED DURABILITY "
              f"({exc.path})")
        print(f"           {exc}")
        print("           do NOT re-ask to 'retry' — the record above is "
              "already published; verify the file after any crash.")
    except Exception as exc:
        # Pre-publication failure or indeterminate state: no verified
        # record of THIS run exists at a known path.
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
    runs_dir_ = _runs_dir()
    exact = runs_dir_ / f"{run_id}.json"
    if exact.is_file():
        return json.loads(exact.read_text(encoding="utf-8")), exact
    runs = sorted(runs_dir_.glob(f"{run_id}*.json"))
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
