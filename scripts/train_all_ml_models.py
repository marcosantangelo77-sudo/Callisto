"""Train an XGBoost baseline for every ``(sport, market)`` with enough data.

Runnable one-shot; safe to re-run (each run writes a new timestamped file).
Reports a summary table to stdout; does NOT mutate the live DB.

Usage
-----
    python scripts/train_all_ml_models.py
    python scripts/train_all_ml_models.py --min-prop-samples 800
    python scripts/train_all_ml_models.py --only-sport basketball_nba
    python scripts/train_all_ml_models.py --only-totals
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Ensure imports work whether invoked from repo root or scripts/
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from tools.ml_classifier import (  # noqa: E402
    _open_ro,
    train_prop_classifier,
    train_total_classifier,
)


def _eligible_combos(conn, min_prop_samples: int, min_total_samples: int):
    """Return (prop_combos, total_sports) that clear the sample minimums."""
    cur = conn.execute(
        """
        SELECT sport, stat_type, COUNT(*) AS n
          FROM player_stats
         WHERE event_id IS NOT NULL
         GROUP BY sport, stat_type
         HAVING n >= ?
         ORDER BY n DESC
        """,
        (min_prop_samples,),
    )
    prop_combos = [(r[0], r[1], int(r[2])) for r in cur.fetchall()]

    cur = conn.execute(
        """
        SELECT be.sport, COUNT(DISTINCT be.event_id) AS n
          FROM backtest_events be
          JOIN game_contexts gc ON gc.sport=be.sport AND gc.event_id=be.event_id
          JOIN game_results gr ON gr.sport=be.sport
               AND gr.home_team=gc.home_team AND gr.away_team=gc.away_team
               AND COALESCE(gr.local_game_date, gr.game_date)
                   = COALESCE(gc.local_game_date, gc.game_date)
         WHERE be.market='totals' AND gr.total_score IS NOT NULL
         GROUP BY be.sport
         HAVING n >= ?
         ORDER BY n DESC
        """,
        (min_total_samples,),
    )
    total_combos = [(r[0], int(r[1])) for r in cur.fetchall()]
    return prop_combos, total_combos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-prop-samples", type=int, default=500)
    ap.add_argument("--min-total-samples", type=int, default=2000)
    ap.add_argument("--max-prop-samples", type=int, default=None,
                    help="Optional cap per (sport, stat) to bound training time.")
    ap.add_argument("--max-total-samples", type=int, default=None)
    ap.add_argument("--only-sport", default=None)
    ap.add_argument("--only-stat", default=None)
    ap.add_argument("--only-totals", action="store_true")
    ap.add_argument("--only-props", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("train_all_ml_models")

    conn = _open_ro()
    try:
        prop_combos, total_sports = _eligible_combos(
            conn,
            min_prop_samples=args.min_prop_samples,
            min_total_samples=args.min_total_samples,
        )
    finally:
        conn.close()

    if args.only_sport:
        prop_combos = [c for c in prop_combos if c[0] == args.only_sport]
        total_sports = [c for c in total_sports if c[0] == args.only_sport]
    if args.only_stat:
        prop_combos = [c for c in prop_combos if c[1] == args.only_stat]

    log.info(
        "eligible: %d prop combos, %d totals sports",
        len(prop_combos), len(total_sports),
    )

    results: list[dict] = []

    # Props
    if not args.only_totals:
        for sport, stat_type, n in prop_combos:
            t0 = time.monotonic()
            log.info("TRAIN prop %s/%s (n=%d)", sport, stat_type, n)
            try:
                tm = train_prop_classifier(
                    sport=sport,
                    stat_type=stat_type,
                    min_samples=args.min_prop_samples,
                    max_samples=args.max_prop_samples,
                )
            except Exception as exc:
                log.exception("TRAIN failed prop %s/%s: %s", sport, stat_type, exc)
                results.append(
                    {
                        "kind": "prop",
                        "sport": sport,
                        "market": f"player_prop_{stat_type}",
                        "status": "error",
                        "error": str(exc),
                    }
                )
                continue
            elapsed = time.monotonic() - t0
            if tm is None:
                results.append(
                    {
                        "kind": "prop",
                        "sport": sport,
                        "market": f"player_prop_{stat_type}",
                        "status": "skipped_low_samples",
                        "n_eligible": n,
                    }
                )
                continue
            results.append(
                {
                    "kind": "prop",
                    "sport": tm.sport,
                    "market": tm.market,
                    "n_train": tm.n_train,
                    "n_test": tm.n_test,
                    "threshold": tm.threshold,
                    "cv_auc_mean": tm.metrics.get("cv_auc_mean"),
                    "cv_brier_mean": tm.metrics.get("cv_brier_mean"),
                    "cv_log_loss_mean": tm.metrics.get("cv_log_loss_mean"),
                    "top_features": tm.feature_importances[:10],
                    "elapsed_s": round(elapsed, 2),
                    "trained_at": tm.trained_at,
                }
            )

    # Totals
    if not args.only_props:
        for sport, n in total_sports:
            t0 = time.monotonic()
            log.info("TRAIN totals %s (n=%d)", sport, n)
            try:
                tm = train_total_classifier(
                    sport=sport,
                    min_samples=args.min_total_samples,
                    max_samples=args.max_total_samples,
                )
            except Exception as exc:
                log.exception("TRAIN failed totals %s: %s", sport, exc)
                results.append(
                    {
                        "kind": "totals",
                        "sport": sport,
                        "market": "totals",
                        "status": "error",
                        "error": str(exc),
                    }
                )
                continue
            elapsed = time.monotonic() - t0
            if tm is None:
                results.append(
                    {
                        "kind": "totals",
                        "sport": sport,
                        "market": "totals",
                        "status": "skipped_low_samples",
                        "n_eligible": n,
                    }
                )
                continue
            results.append(
                {
                    "kind": "totals",
                    "sport": tm.sport,
                    "market": tm.market,
                    "n_train": tm.n_train,
                    "n_test": tm.n_test,
                    "threshold": tm.threshold,
                    "cv_auc_mean": tm.metrics.get("cv_auc_mean"),
                    "cv_brier_mean": tm.metrics.get("cv_brier_mean"),
                    "cv_log_loss_mean": tm.metrics.get("cv_log_loss_mean"),
                    "top_features": tm.feature_importances[:10],
                    "elapsed_s": round(elapsed, 2),
                    "trained_at": tm.trained_at,
                }
            )

    # Print summary
    print()
    print("=" * 88)
    print(f"{'kind':<6} {'sport':<22} {'market':<26} {'n_tr':>6} {'auc':>7} {'brier':>7} {'logl':>7}")
    print("-" * 88)
    for r in results:
        if r.get("status") in {"error", "skipped_low_samples"}:
            print(f"{r.get('kind','?'):<6} {r.get('sport',''):<22} {r.get('market',''):<26} {'-':>6} {'-':>7} {'-':>7} {'-':>7}  [{r.get('status')}]")
            continue
        auc = r.get("cv_auc_mean")
        brier = r.get("cv_brier_mean")
        ll = r.get("cv_log_loss_mean")
        print(
            f"{r['kind']:<6} {r['sport']:<22} {r['market']:<26} {r['n_train']:>6} "
            f"{auc if auc is not None else 'n/a':>7} "
            f"{brier if brier is not None else 'n/a':>7} "
            f"{ll if ll is not None else 'n/a':>7}"
        )
    print("=" * 88)
    print()
    print("Top features by model:")
    for r in results:
        tops = r.get("top_features")
        if not tops:
            continue
        names = ", ".join(f"{n}({imp:.3f})" for n, imp in tops[:5])
        print(f"  {r['sport']}/{r['market']}: {names}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
