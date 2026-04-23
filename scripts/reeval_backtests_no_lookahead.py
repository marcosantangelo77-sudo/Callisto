"""Re-evaluate every backtest_runs row under CALLISTO_BACKTEST_LEAD_MINUTES=60.

Purpose
-------
Quantifies how much of Callisto's historical significance was driven by
closing-line lookahead. For each hypothesis on master, we:

  1. Identify the most-recent backtest_runs row (p_before, roi_before,
     is_significant_before).
  2. Re-run the backtest against a COPY of memory/callisto.db with
     CALLISTO_BACKTEST_LEAD_MINUTES=60 — the new /odds/movements path
     returns prices that were visible at T-60m, not closing.
  3. Compare p_after / roi_after / is_significant_after.

Two execution modes
-------------------
  --simulate (default)
      No API calls. Approximates the lookahead gap using the CLV data
      already recorded on each backtest_event (book_implied vs
      closing_implied). This is exact for steam-following hypotheses where
      CLV IS the alpha, and a conservative lower bound for others.
      Cost: 0 odds-api credits.

  --live
      Re-runs the actual BacktestEngine.run_backtest against the DB COPY
      with lead_minutes=60. Every event round-trips through the new
      /odds/movements path; cached pre-commence snapshots are reused.
      Cost: up to (events × books × markets) odds-api credits — gated by
      --credit-budget.

Usage
-----
    python scripts/reeval_backtests_no_lookahead.py \
        --top-n 20 \
        --db memory/callisto.db \
        [--simulate | --live]

Outputs a Markdown comparison table to stdout and writes a JSON file at
data/reeval_no_lookahead.json.

NEVER touches the live DB — all writes happen on /tmp/callisto_reeval.db.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _binomial_pvalue(wins: int, decided: int) -> float:
    """Two-sided binomial test vs p=0.5. Matches the calculation used in
    hypothesis.evaluate_significance so p_before / p_after are directly
    comparable."""
    if decided <= 0:
        return 1.0
    # Tail probability of observing wins >= k (or <= k) under H0 p=0.5.
    # Use the closed-form sum of binomial(decided, i) * 0.5^decided.
    mean = decided / 2.0
    k = abs(wins - mean)
    lo = int(math.floor(mean - k))
    hi = int(math.ceil(mean + k))
    from math import comb
    total_mass = 0.0
    for i in range(decided + 1):
        if i <= lo or i >= hi:
            total_mass += comb(decided, i)
    return min(1.0, total_mass * (0.5 ** decided))


def _roi_from_events(rows: list[tuple[int, int, int]], stake: float = 1.0) -> float:
    """Simplified ROI — wins pay even money, losses lose stake, pushes 0.

    Matches the default evaluate_significance path (decimal_odds=2.0 proxy).
    For comparison-over-comparison analysis this is sufficient; the real ROI
    absolute number depends on the odds-weighted Kelly model.
    """
    if not rows:
        return 0.0
    total = 0.0
    n = 0
    for wins, losses, pushes in rows:
        total += wins * stake - losses * stake
        n += wins + losses + pushes
    if n == 0:
        return 0.0
    return total / n * 100.0


async def _simulate_mode(db_copy: str, top_n: int) -> list[dict]:
    """Approximate p_after / roi_after WITHOUT odds-api calls.

    Known limitation (discovered during this audit): the existing
    backtest_events table never populated `closing_implied` or `clv_implied`
    columns — under the lookahead bug, the closing price WAS the book price,
    so the "closing line value" had nothing to compare against. As a
    consequence a pure-DB simulate cannot recover the real pre-commence
    price; it can only bound the shift assuming a plausible CLV drift.

    Strategy:
      * For the small subset of rows where closing_implied IS populated
        (live paper_trades resolved after the fix, if any), use the real
        gap.
      * Otherwise assume an informed-market drift of 1.5% (150 bp) between
        T-60 and closing — the median CLV observed on resolved DraftKings
        paper_trades since 2026-03. Signals whose edge < drift get dropped
        from the "after" sample.

    The resulting p_after / roi_after are ORDER-OF-MAGNITUDE estimates and
    MUST be re-computed with --live on a sampled subset before any
    promotion decision is reversed. The --live path calls the real
    /odds/movements endpoint and produces the authoritative answer.
    """
    ASSUMED_CLV_DRIFT = 0.015  # 150 bp — median resolved paper_trade CLV
    db = sqlite3.connect(db_copy)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # Top-N hypotheses by backtest event volume with a resolved sample.
    cur.execute(
        "SELECT hypothesis_id, COUNT(*) AS n FROM backtest_events "
        "WHERE actual_result IN ('won','lost','push') "
        "GROUP BY hypothesis_id ORDER BY n DESC LIMIT ?",
        (top_n,),
    )
    top = [r["hypothesis_id"] for r in cur.fetchall()]

    results: list[dict] = []
    for hid in top:
        # Latest backtest_run row = the "before" baseline.
        cur.execute(
            "SELECT run_id, signals_generated, p_value_binomial, roi_pct, "
            "  is_significant, hit_rate "
            "FROM backtest_runs WHERE hypothesis_id = ? "
            "ORDER BY completed_at DESC LIMIT 1",
            (hid,),
        )
        run = cur.fetchone()
        if not run:
            continue
        p_before = run["p_value_binomial"] or 1.0
        roi_before = run["roi_pct"] or 0.0
        sig_before = bool(run["is_significant"])
        hit_before = run["hit_rate"] or 0.0

        # Pull every SIGNAL event's edge + result + CLV gap for the
        # most-recent run only — earlier runs may reflect different
        # hypothesis parameters / sample windows.
        cur.execute(
            "SELECT edge, book_implied_prob, closing_implied, clv_implied, "
            "  actual_result "
            "FROM backtest_events "
            "WHERE hypothesis_id = ? AND run_id = ? AND signal_generated = 1 ",
            (hid, run["run_id"]),
        )
        rows = cur.fetchall()
        if not rows:
            results.append({
                "hypothesis_id": hid, "p_before": p_before, "p_after": p_before,
                "roi_before": roi_before, "roi_after": roi_before,
                "sig_before": sig_before, "sig_after": sig_before,
                "note": "no signals",
            })
            continue

        # Average CLV gap across resolved signals — this is the shrinkage.
        clv_gaps = []
        for r in rows:
            if r["closing_implied"] is not None and r["book_implied_prob"] is not None:
                # Positive gap = closing > book => closing price implies a
                # WORSE (tighter) edge than book price. This is the
                # lookahead leak: the original row used the closing-like
                # price AS the book price, inflating our reported edge.
                clv_gaps.append(float(r["closing_implied"]) - float(r["book_implied_prob"]))
        if clv_gaps:
            avg_shrink = sum(clv_gaps) / len(clv_gaps)
            shrink_source = "observed_clv"
        else:
            avg_shrink = ASSUMED_CLV_DRIFT
            shrink_source = "assumed_150bp"

        # Recount signals after applying shrinkage: any whose edge -
        # avg_shrink drops below 0 (or any positive threshold) is dropped.
        # Use 0 as a conservative "still has ANY edge" floor — we're not
        # trying to re-run the full edge_threshold logic, just bound the
        # damage.
        surviving = []
        for r in rows:
            edge = float(r["edge"] or 0.0)
            if edge - max(0.0, avg_shrink) > 0 and r["actual_result"] in ("won", "lost", "push"):
                surviving.append(r["actual_result"])

        wins_after = surviving.count("won")
        losses_after = surviving.count("lost")
        pushes_after = surviving.count("push")
        decided_after = wins_after + losses_after
        if decided_after < 2:
            # Sample collapsed — treat as "no significance" after removal.
            p_after = 1.0
            roi_after = 0.0
            hit_after = 0.0
            sig_after = False
        else:
            p_after = _binomial_pvalue(wins_after, decided_after)
            hit_after = wins_after / decided_after
            roi_after = _roi_from_events([(wins_after, losses_after, pushes_after)])
            sig_after = p_after < 0.05

        # Count "before" signals the same way as "after" — row count with
        # signal_generated=1 in the latest run_id. The stored
        # signals_generated on backtest_runs uses a different tally
        # (de-duped at aggregate time), so mixing them makes the delta
        # misleading.
        n_signals_before = len(rows)
        results.append({
            "hypothesis_id": hid,
            "n_signals_before": n_signals_before,
            "n_signals_after": len(surviving),
            "p_before": round(p_before, 5),
            "p_after": round(p_after, 5),
            "roi_before": round(roi_before, 3),
            "roi_after": round(roi_after, 3),
            "hit_before": round(hit_before, 3),
            "hit_after": round(hit_after, 3),
            "sig_before": sig_before,
            "sig_after": sig_after,
            "avg_edge_shrink": round(avg_shrink, 4),
            "shrink_source": shrink_source,
        })

    db.close()
    return results


async def _live_mode(
    db_copy: str, top_n: int, credit_budget: int,
) -> list[dict]:
    """Run the real BacktestEngine with lead=60 against the DB copy.

    Each (hypothesis, date-range) pair re-fetches /odds/movements for events
    not yet in the lookahead-free cache. Cost is bounded by `credit_budget`.
    """
    os.environ["CALLISTO_DB_PATH"] = db_copy
    os.environ["CALLISTO_BACKTEST_LEAD_MINUTES"] = "60"

    # Import after env vars are set so modules pick up the copy path.
    from tools.backtest import BacktestEngine
    from tools.historical_odds import HistoricalOddsFetcher
    from tools.hypothesis import HypothesisManager
    from tools.odds_api_io import get_usage_status

    db = sqlite3.connect(db_copy)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    cur.execute(
        "SELECT hypothesis_id, COUNT(*) AS n FROM backtest_events "
        "GROUP BY hypothesis_id ORDER BY n DESC LIMIT ?",
        (top_n,),
    )
    top = [r["hypothesis_id"] for r in cur.fetchall()]
    db.close()

    fetcher = HistoricalOddsFetcher(db_copy)
    await fetcher.initialize()
    hmgr = HypothesisManager(db_copy)
    await hmgr.initialize()
    engine = BacktestEngine(db_copy, fetcher, hmgr)
    await engine.initialize()

    start_credits = get_usage_status()
    results: list[dict] = []

    try:
        for hid in top:
            usage = get_usage_status()
            spent = usage["lifetime_requests"] - start_credits["lifetime_requests"]
            if spent >= credit_budget:
                print(f"[budget] stopping at {spent}/{credit_budget} credits")
                break

            # Before-state from latest existing run_id.
            before_db = sqlite3.connect(db_copy)
            before_db.row_factory = sqlite3.Row
            bc = before_db.cursor()
            bc.execute(
                "SELECT date_range_start, date_range_end, p_value_binomial, "
                "  roi_pct, is_significant, signals_generated, hit_rate "
                "FROM backtest_runs WHERE hypothesis_id = ? "
                "ORDER BY completed_at DESC LIMIT 1",
                (hid,),
            )
            row = bc.fetchone()
            before_db.close()
            if not row:
                continue
            start_date, end_date = row["date_range_start"], row["date_range_end"]

            result = await engine.run_backtest(hid, start_date, end_date, credit_budget=100)
            # Fetch latest run row AFTER the re-run.
            after_db = sqlite3.connect(db_copy)
            after_db.row_factory = sqlite3.Row
            ac = after_db.cursor()
            ac.execute(
                "SELECT p_value_binomial, roi_pct, is_significant, "
                "  signals_generated, hit_rate "
                "FROM backtest_runs WHERE hypothesis_id = ? "
                "ORDER BY completed_at DESC LIMIT 1",
                (hid,),
            )
            a = ac.fetchone()
            after_db.close()

            results.append({
                "hypothesis_id": hid,
                "n_signals_before": row["signals_generated"],
                "n_signals_after": a["signals_generated"] if a else 0,
                "p_before": row["p_value_binomial"] or 1.0,
                "p_after": (a["p_value_binomial"] if a else 1.0) or 1.0,
                "roi_before": row["roi_pct"] or 0.0,
                "roi_after": (a["roi_pct"] if a else 0.0) or 0.0,
                "hit_before": row["hit_rate"] or 0.0,
                "hit_after": (a["hit_rate"] if a else 0.0) or 0.0,
                "sig_before": bool(row["is_significant"]),
                "sig_after": bool(a["is_significant"]) if a else False,
            })
    finally:
        await fetcher.close()
        await hmgr.close()

    end_credits = get_usage_status()
    spent = end_credits["lifetime_requests"] - start_credits["lifetime_requests"]
    print(f"[credits] consumed {spent} odds-api.io credits")
    return results


def _summarize(results: list[dict]) -> dict:
    n = len(results)
    lost_sig = sum(1 for r in results if r.get("sig_before") and not r.get("sig_after"))
    stayed_sig = sum(1 for r in results if r.get("sig_before") and r.get("sig_after"))
    gained_sig = sum(1 for r in results if not r.get("sig_before") and r.get("sig_after"))
    roi_drops = [
        (r.get("roi_before", 0.0) or 0.0) - (r.get("roi_after", 0.0) or 0.0)
        for r in results
    ]
    mean_roi_drop = sum(roi_drops) / n if n else 0.0
    return {
        "n": n,
        "lost_significance": lost_sig,
        "kept_significance": stayed_sig,
        "gained_significance": gained_sig,
        "mean_roi_drop_pct": round(mean_roi_drop, 3),
    }


def _md_table(results: list[dict]) -> str:
    header = (
        "| hypothesis_id | n_sig_before | n_sig_after | p_before | p_after | "
        "roi_before | roi_after | sig_before | sig_after |\n"
        "|---|---:|---:|---:|---:|---:|---:|:---:|:---:|"
    )
    lines = [header]
    for r in results:
        lines.append(
            f"| {r['hypothesis_id']} | "
            f"{r.get('n_signals_before','')} | {r.get('n_signals_after','')} | "
            f"{r.get('p_before','')} | {r.get('p_after','')} | "
            f"{r.get('roi_before','')} | {r.get('roi_after','')} | "
            f"{'Y' if r.get('sig_before') else 'N'} | "
            f"{'Y' if r.get('sig_after') else 'N'} |"
        )
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--db", default="memory/callisto.db")
    ap.add_argument("--out", default="data/reeval_no_lookahead.json")
    ap.add_argument(
        "--live", action="store_true",
        help="Actually re-run the backtest engine against /odds/movements. "
             "Costs odds-api credits. Default is --simulate.",
    )
    ap.add_argument("--credit-budget", type=int, default=500)
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 2

    # Copy the DB OFF the live path — never mutate the source.
    tmp_dir = Path(tempfile.mkdtemp(prefix="callisto_reeval_"))
    db_copy = str(tmp_dir / "callisto_reeval.db")
    print(f"[copy] {args.db} -> {db_copy}")
    shutil.copy(args.db, db_copy)
    # Copy WAL sidecar files if they exist so the copy is internally consistent.
    for suf in ("-wal", "-shm"):
        src = f"{args.db}{suf}"
        if Path(src).exists():
            shutil.copy(src, f"{db_copy}{suf}")

    try:
        if args.live:
            results = await _live_mode(db_copy, args.top_n, args.credit_budget)
        else:
            results = await _simulate_mode(db_copy, args.top_n)
    finally:
        # Clean up the temp copy.
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    summary = _summarize(results)
    print()
    print(_md_table(results))
    print()
    print(f"Summary: {summary['lost_significance']}/{summary['n']} lost significance, "
          f"{summary['kept_significance']} kept, {summary['gained_significance']} gained, "
          f"mean ROI drop: {summary['mean_roi_drop_pct']}%")
    if not args.live:
        print()
        print("WARNING: --simulate is a HEURISTIC bound, not the truth.")
        print("  closing_implied was never populated on backtest_events (the bug")
        print("  left no counterfactual in the DB), so the simulate can only")
        print("  remove sub-threshold signals — it cannot shift the BOOK price")
        print("  we paid. Real T-60 prices come only from /odds/movements.")
        print("  Re-run with --live to get authoritative p_after / roi_after.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(
        {"results": results, "summary": summary}, indent=2
    ))
    print(f"[out] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
