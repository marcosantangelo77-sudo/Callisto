"""One-time LIVE-cascade migration (audit 2026-04-22).

The new paper_trading→live gates (merged on master) enforce
``min_paper_trades``, Šidák FWER, portfolio_correlation ≤ 40%, and
snapshot_quality ≥ 80% pre_commence.  The 22 existing LIVE rows were
grandfathered past these checks; ``_live_overlap_audit.py`` already
showed ~16 of them fail the portfolio-correlation gate alone.

This script re-evaluates every ``status='live'`` hypothesis against the
full ``check_promotion_readiness(status_override='paper_trading')``
gate — the exact code path a fresh candidate would hit today — and, when
a gate fails, demotes the row to ``status='paused'`` with:

  * A ``demotion_reason`` stored in ``hypotheses.notes`` as JSON
    (full reason text + primary category + ``legacy_grandfather=False``
    flag indicating this is a true failure, not a legacy waiver).
  * A ``hypothesis_stats`` row with ``stage='live_demoted'``.
  * A ``wiki_articles`` entry titled ``LIVE cascade demotion: <name>``
    so the research trail sees the state change.
  * A pre-mutation snapshot written to
    ``memory/live_cascade_backup_<ts>.json`` for ``--rollback``.
  * An optional Telegram summary.

Exit codes:
  0  clean run (dry-run or live)
  2  mutation attempted without ``--yes``
  3  rollback file missing / malformed
  4  DB connect failure

CLI examples:
  # Default: dry-run.  Prints the 22-row verdict table.
  python scripts/demote_stale_live_hypotheses.py

  # Filter to a single category.
  python scripts/demote_stale_live_hypotheses.py --reason-filter portfolio_correlation

  # Actually mutate (requires BOTH flags):
  python scripts/demote_stale_live_hypotheses.py --live --yes

  # Roll back a prior cascade:
  python scripts/demote_stale_live_hypotheses.py --rollback \
      --backup memory/live_cascade_backup_1714011234.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Repo root on sys.path so `tools.*` imports work when run as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.hypothesis import HypothesisManager  # noqa: E402

logger = logging.getLogger("callisto.cascade_demote")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ───────── reason categories (mapped from check_promotion_readiness) ─────────
#
# Each FAIL line emitted by check_promotion_readiness starts with one of a
# small set of stable substrings.  Category names match the --reason-filter
# CLI values so the filter can target a single failure mode.
CATEGORY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("portfolio_correlation", re.compile(r"portfolio_correlation_too_high", re.I)),
    ("single_regime_sample", re.compile(r"single_regime_sample", re.I)),
    ("paper_trade_sample", re.compile(r"paper_trade_sample_insufficient", re.I)),
    ("snapshot_quality", re.compile(r"snapshot_quality sample only", re.I)),
    ("min_days", re.compile(r"insufficient_time_in_stage", re.I)),
    ("low_clv", re.compile(r"CLV rate .* < ", re.I)),
    ("p_value", re.compile(r"p-value .* > ", re.I)),
    ("signals", re.compile(r"FAIL:\s*\d+/\d+ signals", re.I)),
    ("brier", re.compile(r"Brier score .* > ", re.I)),
    ("ic", re.compile(r"IC .* < ", re.I)),
    ("drawdown", re.compile(r"Drawdown .* > ", re.I)),
    ("edge_distribution", re.compile(r"signal edge distribution is negative", re.I)),
    ("sortino", re.compile(r"Sortino .* < ", re.I)),
]


def categorize_failures(checks: list[str]) -> list[str]:
    """Return unique category labels for every FAIL line in ``checks``."""
    out: list[str] = []
    for line in checks:
        if not line.startswith("FAIL:"):
            continue
        for label, pat in CATEGORY_PATTERNS:
            if pat.search(line):
                if label not in out:
                    out.append(label)
                break
        else:
            # Catch-all so nothing slips silently.
            if "other" not in out:
                out.append("other")
    return out


# ───────── CLI ─────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="One-time LIVE→paused cascade re-evaluation",
    )
    p.add_argument("--live", action="store_true", help="Actually mutate the DB (default: dry-run).")
    p.add_argument(
        "--yes",
        action="store_true",
        help="Required alongside --live to proceed.  Without this, --live refuses to run.",
    )
    p.add_argument("--limit", type=int, default=None, help="Cap the number of LIVE rows evaluated.")
    p.add_argument(
        "--reason-filter",
        type=str,
        default=None,
        help=(
            "Only demote when the primary failure category matches. "
            "Values: portfolio_correlation, paper_trade_sample, snapshot_quality, "
            "min_days, low_clv, p_value, signals, brier, ic, drawdown, "
            "edge_distribution, sortino, other."
        ),
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
        "--backup-dir",
        type=str,
        default=str(_REPO_ROOT / "memory"),
        help="Directory for pre-mutation snapshot files (default: ./memory/).",
    )
    p.add_argument("--rollback", action="store_true", help="Restore from a backup JSON.")
    p.add_argument(
        "--backup",
        type=str,
        default=None,
        help="Path to the backup JSON used by --rollback (required with --rollback).",
    )
    p.add_argument(
        "--no-telegram",
        action="store_true",
        help="Skip Telegram summary alert even when TELEGRAM_BOT_TOKEN is set.",
    )
    return p


# ───────── evaluation ─────────


async def evaluate_live_row(mgr: HypothesisManager, hyp: dict) -> dict:
    """Re-run paper→live gate against a currently-LIVE hypothesis.

    Returns a per-row verdict:
        {
          hypothesis_id, name, sport, market_type, promoted_at,
          current_status, would_demote (bool), categories (list[str]),
          reasons (list[str]), readiness (dict, raw),
        }
    """
    hid = hyp["hypothesis_id"]
    readiness = await mgr.check_promotion_readiness(
        hid, status_override="paper_trading"
    )

    checks = readiness.get("checks") or []
    categories = categorize_failures(checks)
    fail_lines = [c for c in checks if c.startswith("FAIL:")]
    would_demote = not readiness.get("ready", False) and bool(fail_lines or categories)

    return {
        "hypothesis_id": hid,
        "name": hyp.get("name"),
        "sport": hyp.get("sport"),
        "market_type": hyp.get("market_type"),
        "promoted_at": hyp.get("promoted_at"),
        "current_status": hyp.get("status"),
        "would_demote": would_demote,
        "categories": categories,
        "reasons": fail_lines,
        "checks": checks,
        "readiness_ready": bool(readiness.get("ready")),
        "readiness_reason": readiness.get("reason"),
    }


# ───────── mutation ─────────


async def apply_demotion(mgr: HypothesisManager, verdict: dict) -> dict:
    """Demote a single LIVE row → paused.  Returns outcome dict."""
    hid = verdict["hypothesis_id"]
    categories = verdict["categories"]
    primary = categories[0] if categories else "unknown"
    reason_text = " | ".join(verdict["reasons"])[:900]

    # 1) CAS status flip: live → paused
    cas = await mgr.update_status(
        hid,
        "paused",
        "cascade_demote_2026_04_22",
        expected_status="live",
    )
    if not cas.get("changed"):
        logger.warning(f"{hid}: CAS no-op (row already moved)")
        return {"hypothesis_id": hid, "changed": False, "reason": "cas_noop"}

    # 2) Persist the demotion reason onto the row.
    #    hypotheses.notes is a free-text column; we overwrite it with a
    #    structured JSON payload so downstream tools can parse it.
    now_iso = datetime.now(timezone.utc).isoformat()
    notes_payload = {
        "demotion_reason": primary,
        "demotion_categories": categories,
        "demotion_full_text": reason_text,
        "demoted_at": now_iso,
        "demoted_by": "cascade_demote_2026_04_22",
        "legacy_grandfather": False,  # true failure, not waived
        "previous_status": "live",
    }
    await mgr._db.execute(
        "UPDATE hypotheses SET notes = ?, updated_at = ? WHERE hypothesis_id = ?",
        (json.dumps(notes_payload), now_iso, hid),
    )

    # 3) Clear _use_backtest_evidence if present in model_config
    cur = await mgr._db.execute(
        "SELECT model_config FROM hypotheses WHERE hypothesis_id = ?", (hid,),
    )
    row = await cur.fetchone()
    if row and row[0]:
        try:
            mc = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            mc = {}
        if isinstance(mc, dict) and mc.pop("_use_backtest_evidence", None) is not None:
            await mgr._db.execute(
                "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                (json.dumps(mc), hid),
            )

    # 4) hypothesis_stats insert — stage='live_demoted'
    rs = verdict.get("readiness_reason") or ""
    await mgr._db.execute(
        "INSERT INTO hypothesis_stats "
        "(hypothesis_id, stage, computed_at, total_n, signals_n, "
        " win, loss, push_, hit_rate, avg_clv, positive_clv_rate, "
        " roi_pct, max_drawdown, sortino, is_significant) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hid,
            "live_demoted",
            now_iso,
            0, 0, 0, 0, 0,
            None, None, None, None, None, None,
            False,
        ),
    )

    # 5) Wiki article (best-effort — schema may vary).  Use
    #    INSERT OR REPLACE since the topic key is the hypothesis_id and
    #    a second cascade pass should update, not crash.
    try:
        wiki_body = (
            f"# LIVE cascade demotion: {verdict['name']}\n\n"
            f"**Hypothesis:** `{hid}` ({verdict['sport']}, {verdict['market_type']})\n"
            f"**Demoted at:** {now_iso}\n"
            f"**Primary reason:** `{primary}`\n"
            f"**All categories:** {', '.join(categories) or 'n/a'}\n\n"
            f"## Full gate output\n\n"
            + "\n".join(f"- {c}" for c in verdict["checks"])
        )
        topic = f"live_cascade_demotion_{hid}"
        await mgr._db.execute(
            "INSERT OR REPLACE INTO wiki_articles "
            "(topic, title, content, summary, related_topics, source_sessions, "
            " source_entries, domain, confidence, created_at, updated_at, "
            " compile_count, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                topic,
                f"LIVE cascade demotion: {verdict['name']}",
                wiki_body,
                f"Demoted from LIVE → paused on 2026-04-22 ({primary}).",
                json.dumps([]),
                json.dumps([]),
                json.dumps([]),
                "SIGNAL",
                0.95,
                now_iso,
                now_iso,
                1,
                "",
            ),
        )
    except Exception as e:
        logger.warning(f"{hid}: wiki insert failed: {e}")

    await mgr._db.commit()
    logger.info(f"{hid}: demoted → paused (reason={primary})")
    return {"hypothesis_id": hid, "changed": True, "primary": primary}


# ───────── backup / rollback ─────────


async def snapshot_pre_cascade(mgr: HypothesisManager, backup_dir: Path) -> Path:
    """Dump every LIVE row (and any already-paused with the cascade
    marker) to a JSON file before any mutation runs.  Used by ``--rollback``.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    cur = await mgr._db.execute(
        "SELECT hypothesis_id, name, status, notes, model_config, "
        "promoted_at, promoted_by, updated_at "
        "FROM hypotheses "
        "WHERE status IN ('live','paused')"
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "script": "demote_stale_live_hypotheses.py",
        "rows": [dict(zip(cols, r)) for r in rows],
    }
    path = backup_dir / f"live_cascade_backup_{int(time.time())}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info(f"snapshot written: {path} ({len(rows)} rows)")
    return path


async def rollback_from_backup(mgr: HypothesisManager, backup_path: Path) -> dict:
    """Restore status/notes/model_config from a snapshot.  Only touches
    rows whose current status is `paused` with the cascade demoted_by tag
    so an ad-hoc paused row isn't clobbered.
    """
    if not backup_path.exists():
        return {"error": f"backup not found: {backup_path}", "exit": 3}
    try:
        payload = json.loads(backup_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"backup unreadable: {e}", "exit": 3}

    rows = payload.get("rows") or []
    restored = 0
    skipped = 0
    for snap in rows:
        hid = snap["hypothesis_id"]
        was_status = snap.get("status")
        if was_status != "live":
            continue  # only restore rows that were LIVE pre-cascade
        cur = await mgr._db.execute(
            "SELECT status, promoted_by FROM hypotheses WHERE hypothesis_id = ?",
            (hid,),
        )
        cur_row = await cur.fetchone()
        if not cur_row:
            skipped += 1
            continue
        cur_status, cur_promoted_by = cur_row
        if cur_status != "paused" or cur_promoted_by != "cascade_demote_2026_04_22":
            logger.warning(
                f"{hid}: skipping rollback (status={cur_status}, by={cur_promoted_by})"
            )
            skipped += 1
            continue
        await mgr._db.execute(
            "UPDATE hypotheses SET status = ?, notes = ?, model_config = ?, "
            "promoted_at = ?, promoted_by = ?, updated_at = ? "
            "WHERE hypothesis_id = ? AND status = 'paused'",
            (
                "live",
                snap.get("notes"),
                snap.get("model_config"),
                snap.get("promoted_at"),
                snap.get("promoted_by"),
                datetime.now(timezone.utc).isoformat(),
                hid,
            ),
        )
        restored += 1
    await mgr._db.commit()
    return {"restored": restored, "skipped": skipped, "source": str(backup_path)}


# ───────── reporting ─────────


def print_table(verdicts: list[dict]) -> None:
    """Structured dry-run output."""
    header = (
        f"{'hypothesis_id':14s}  {'name':48s}  {'state':10s}  "
        f"{'demote':6s}  reasons"
    )
    print(header)
    print("-" * len(header) + "  …")
    for v in verdicts:
        cats = ",".join(v["categories"]) or "-"
        print(
            f"{v['hypothesis_id']:14s}  "
            f"{(v['name'] or '')[:48]:48s}  "
            f"{v['current_status']:10s}  "
            f"{('YES' if v['would_demote'] else 'no'):6s}  "
            f"{cats}"
        )


def print_distribution(verdicts: list[dict]) -> None:
    counts: dict[str, int] = {}
    multi = 0
    for v in verdicts:
        if not v["would_demote"]:
            continue
        for cat in v["categories"]:
            counts[cat] = counts.get(cat, 0) + 1
        if len(v["categories"]) > 1:
            multi += 1
    print("\nDistribution of demotion reasons (primary + secondary):")
    for k, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:24s} {n}")
    print(f"  (rows failing multiple gates: {multi})")


def build_telegram_summary(verdicts: list[dict], remaining_live: int) -> str:
    total = len(verdicts)
    demoted = sum(1 for v in verdicts if v["would_demote"])
    cat_primary: dict[str, int] = {}
    for v in verdicts:
        if v["would_demote"] and v["categories"]:
            cat_primary[v["categories"][0]] = cat_primary.get(v["categories"][0], 0) + 1
    lines = [f"LIVE Cascade: {demoted}/{total} demoted to paused"]
    for cat, n in sorted(cat_primary.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {n} failed {cat}")
    lines.append(f"Remaining LIVE: {remaining_live}")
    return "\n".join(lines)


async def maybe_send_telegram(summary: str, disabled: bool) -> None:
    if disabled:
        return
    if not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
        logger.info("Telegram not configured — skipping summary alert")
        return
    try:
        from tools.telegram import send_alert  # noqa: WPS433
        ok = await send_alert(summary, silent=True, parse_mode="HTML")
        logger.info(f"Telegram summary sent: {ok}")
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")


# ───────── main ─────────


async def main_async(args: argparse.Namespace) -> int:
    if args.live and not args.yes:
        print(
            "ERROR: --live requires --yes (explicit confirmation).\n"
            "       This is a major state mutation; dry-run first.",
            file=sys.stderr,
        )
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 4

    # DRY-RUN SAFETY: check_promotion_readiness internally calls
    # evaluate_significance which writes hypothesis_stats rows.  For a
    # true read-only dry-run we operate on a shadow copy of the DB so
    # the live process is never touched.  For --live / --rollback we
    # work against the real path (mutations are the point).
    effective_db = db_path
    shadow_path: Optional[Path] = None
    if not args.live and not args.rollback:
        shadow_path = Path(tempfile.mkdtemp(prefix="cascade_shadow_")) / db_path.name
        # Copy DB + WAL sidecars if present so the shadow is consistent.
        shutil.copy2(db_path, shadow_path)
        for sfx in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + sfx)
            if side.exists():
                shutil.copy2(side, shadow_path.with_name(shadow_path.name + sfx))
        effective_db = shadow_path
        logger.info(f"Dry-run operating on shadow copy: {shadow_path}")

    mgr = HypothesisManager(db_path=str(effective_db))
    try:
        await mgr.initialize()
    except Exception as e:
        print(f"ERROR: DB connect failed: {e}", file=sys.stderr)
        return 4

    try:
        # ── rollback path ──
        if args.rollback:
            if not args.backup:
                print("ERROR: --rollback requires --backup <path>", file=sys.stderr)
                return 3
            result = await rollback_from_backup(mgr, Path(args.backup))
            if "error" in result:
                print(f"ERROR: {result['error']}", file=sys.stderr)
                return result.get("exit", 3)
            print(
                f"Rollback complete: restored={result['restored']}, "
                f"skipped={result['skipped']}, source={result['source']}"
            )
            return 0

        # ── evaluation ──
        live_rows = await mgr.list_hypotheses(status="live")
        if args.limit:
            live_rows = live_rows[: args.limit]

        verdicts: list[dict] = []
        for h in live_rows:
            v = await evaluate_live_row(mgr, h)
            verdicts.append(v)

        # Optional filter
        if args.reason_filter:
            verdicts_for_demote = [
                v for v in verdicts
                if v["would_demote"] and args.reason_filter in v["categories"]
            ]
        else:
            verdicts_for_demote = [v for v in verdicts if v["would_demote"]]

        # ── dry-run output always printed ──
        print(
            f"\nLIVE cascade re-evaluation — {len(verdicts)} rows "
            f"({len(verdicts_for_demote)} would demote)"
        )
        print_table(verdicts)
        print_distribution(verdicts)

        if not args.live:
            print("\nDRY-RUN — no DB mutation performed.  Pass --live --yes to apply.")
            return 0

        # ── live mutation path ──
        snap_path = await snapshot_pre_cascade(mgr, Path(args.backup_dir))
        print(f"\nPre-mutation snapshot: {snap_path}")

        demoted = 0
        for v in verdicts_for_demote:
            outcome = await apply_demotion(mgr, v)
            if outcome.get("changed"):
                demoted += 1

        # Count remaining LIVE rows (CAS race-safe recount)
        cur = await mgr._db.execute(
            "SELECT COUNT(*) FROM hypotheses WHERE status = 'live'"
        )
        remaining_live = (await cur.fetchone())[0]

        print(
            f"\nCascade complete: demoted={demoted}, "
            f"remaining_live={remaining_live}, backup={snap_path}"
        )

        summary = build_telegram_summary(verdicts_for_demote, remaining_live)
        await maybe_send_telegram(summary, disabled=args.no_telegram)
        return 0
    finally:
        await mgr.close()
        if shadow_path is not None:
            try:
                shadow_dir = shadow_path.parent
                for f in shadow_dir.iterdir():
                    try:
                        f.unlink()
                    except OSError:
                        pass
                shadow_dir.rmdir()
            except OSError:
                pass


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
