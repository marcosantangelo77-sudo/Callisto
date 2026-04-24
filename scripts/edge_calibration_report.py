#!/usr/bin/env python
"""Edge confidence calibration report.

Queries historical resolved edges from backtest_events, buckets predicted
probabilities into deciles, computes empirical hit rate per bucket, fits
Platt + Isotonic calibrators on a training split, evaluates on held-out
data, performs a CLV sanity check via closing_lines, writes a JSON report
to reports/, and optionally persists the best-performing calibrator to
memory/edge_calibrator.json for inference-time use.

Usage:
    python scripts/edge_calibration_report.py               # report only
    python scripts/edge_calibration_report.py --install     # + save calibrator
    python scripts/edge_calibration_report.py --db PATH     # alt DB
    python scripts/edge_calibration_report.py --min-rows 500
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.edge_confidence import (
    PlattCalibrator,
    IsotonicCalibrator,
    IdentityCalibrator,
    brier_score,
    expected_calibration_error,
    save_calibrator,
)


def _resolve_db_path(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    env = os.environ.get("CALLISTO_DB_PATH")
    if env and Path(env).exists():
        return env
    candidates = [
        ROOT / "memory" / "callisto.db",
        Path.cwd() / "memory" / "callisto.db",
    ]
    for c in candidates:
        try:
            if c.exists() and c.stat().st_size > 0:
                return str(c)
        except OSError:
            pass
    main_repo = Path(r"C:\Users\marco\OneDrive\Desktop\Callisto\memory\callisto.db")
    if main_repo.exists():
        return str(main_repo)
    return str(candidates[0])


def _load_events(db_path: str, min_rows: int) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    q = """
        SELECT id, sport, market, book, book_odds_american, book_implied_prob,
               model_fair_prob, edge, ev_pct, actual_result,
               event_id, side, game_date, snapshot_time, created_at
          FROM backtest_events
         WHERE actual_result IN ('won','lost')
           AND model_fair_prob IS NOT NULL
           AND book_implied_prob IS NOT NULL
    """
    rows = [dict(r) for r in conn.execute(q).fetchall()]
    if len(rows) < min_rows:
        raise RuntimeError(
            f"Insufficient resolved rows in backtest_events: {len(rows)} < {min_rows}"
        )
    return rows


def _attach_closing(db_path: str, rows: list[dict]) -> int:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    hit = 0
    for r in rows:
        cl = conn.execute(
            """SELECT closing_implied FROM closing_lines
               WHERE event_id = ? AND market = ? AND team = ?
               ORDER BY captured_at DESC LIMIT 1""",
            (r.get("event_id"), r.get("market"), r.get("side")),
        ).fetchone()
        if cl and cl["closing_implied"] is not None:
            r["closing_implied"] = float(cl["closing_implied"])
            hit += 1
        else:
            r["closing_implied"] = None
    return hit


def _split_train_eval(rows: list[dict], train_frac: float = 0.7) -> tuple[list[dict], list[dict]]:
    def _key(r: dict):
        return (r.get("game_date") or r.get("snapshot_time") or r.get("created_at") or "")
    sorted_rows = sorted(rows, key=_key)
    cut = int(len(sorted_rows) * train_frac)
    return sorted_rows[:cut], sorted_rows[cut:]


def _reliability_table(probs: np.ndarray, outcomes: np.ndarray, *, n_bins: int = 10) -> list[dict]:
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    out = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        if i == n_bins - 1:
            mask = (probs >= lo) & (probs <= hi)
        else:
            mask = (probs >= lo) & (probs < hi)
        n = int(mask.sum())
        if n == 0:
            out.append({
                "bin_low": float(lo), "bin_high": float(hi),
                "n": 0, "mean_pred": None, "hit_rate": None, "gap": None,
            })
            continue
        mean_pred = float(probs[mask].mean())
        hit = float(outcomes[mask].mean())
        out.append({
            "bin_low": float(lo),
            "bin_high": float(hi),
            "n": n,
            "mean_pred": round(mean_pred, 4),
            "hit_rate": round(hit, 4),
            "gap": round(hit - mean_pred, 4),
        })
    return out


def _ascii_reliability_diagram(table: list[dict], *, title: str, width: int = 40) -> str:
    lines = [f"  {title}", "  " + "-" * (width + 20)]
    lines.append(f"  {'Bin':<13} {'n':>6}  {'pred':>6}  {'obs':>6}  {'bar':<{width}}")
    for b in table:
        n = b["n"]
        pred = b["mean_pred"]
        obs = b["hit_rate"]
        bin_label = f"[{b['bin_low']:.1f},{b['bin_high']:.1f})"
        if n == 0 or pred is None or obs is None:
            lines.append(f"  {bin_label:<13} {n:>6}  {'--':>6}  {'--':>6}  (empty)")
            continue
        pred_col = int(round(pred * width))
        obs_col = int(round(obs * width))
        bar = [" "] * width
        for pos in range(width):
            if pos == pred_col == obs_col:
                bar[pos] = "X"
            elif pos == pred_col:
                bar[pos] = "|"
            elif pos == obs_col:
                bar[pos] = "*"
        bar_str = "".join(bar)
        lines.append(f"  {bin_label:<13} {n:>6}  {pred:>6.3f}  {obs:>6.3f}  {bar_str}")
    lines.append("  (| = predicted,  * = observed,  X = match)")
    return "\n".join(lines)


def _outcome_vec(rows: list[dict]) -> np.ndarray:
    return np.asarray([1.0 if r["actual_result"] == "won" else 0.0 for r in rows], dtype=float)


def _clv_analysis(rows: list[dict], calibrated_probs: np.ndarray) -> dict:
    clv_list = []
    cal_list = []
    for r, cp in zip(rows, calibrated_probs):
        if r.get("closing_implied") is None:
            continue
        if r.get("book_implied_prob") is None:
            continue
        clv = float(r["closing_implied"]) - float(r["book_implied_prob"])
        clv_list.append(clv)
        cal_list.append(float(cp))
    if len(clv_list) < 10:
        return {"n": len(clv_list), "mean_clv": None, "pearson": None, "trustworthy": None}
    clv_arr = np.asarray(clv_list)
    cal_arr = np.asarray(cal_list)
    mean_clv = float(clv_arr.mean())
    if cal_arr.std() > 0 and clv_arr.std() > 0:
        pearson = float(np.corrcoef(cal_arr, clv_arr)[0, 1])
    else:
        pearson = None
    trustworthy = bool(mean_clv > 0)
    return {
        "n": len(clv_list),
        "mean_clv": round(mean_clv, 6),
        "pearson_cal_vs_clv": round(pearson, 4) if pearson is not None else None,
        "trustworthy": trustworthy,
        "note": "mean CLV > 0 indicates calibration correlates with sharp closing moves; "
                "positive hit rate with negative CLV = stale lines (suspect).",
    }


def _summary_metrics(probs: np.ndarray, y: np.ndarray) -> dict:
    return {
        "n": int(len(probs)),
        "brier": round(brier_score(probs, y), 6) if len(probs) else None,
        "ece": round(expected_calibration_error(probs, y), 6) if len(probs) else None,
        "mean_pred": round(float(probs.mean()), 6) if len(probs) else None,
        "hit_rate": round(float(y.mean()), 6) if len(y) else None,
    }


def _hit_rate_by_source_class(rows: list[dict]) -> dict:
    # Source class proxy from book name (sharp vs non-sharp).
    from tools.edge_confidence import SHARP_BOOKS
    groups = {"PRIMARY": [], "SECONDARY": []}
    for r in rows:
        book = (r.get("book") or "").lower()
        cls = "PRIMARY" if book in SHARP_BOOKS else "SECONDARY"
        y = 1 if r["actual_result"] == "won" else 0
        groups[cls].append((float(r["model_fair_prob"]), y))
    out = {}
    for cls, items in groups.items():
        if not items:
            out[cls] = {"n": 0}
            continue
        p = np.asarray([x[0] for x in items])
        y = np.asarray([x[1] for x in items], dtype=float)
        out[cls] = {
            "n": len(items),
            "mean_pred": round(float(p.mean()), 4),
            "hit_rate": round(float(y.mean()), 4),
            "brier": round(brier_score(p, y), 6),
        }
    return out


def build_report(db_path: str, *, min_rows: int, install: bool) -> dict:
    rows = _load_events(db_path, min_rows=min_rows)
    clv_hit = _attach_closing(db_path, rows)

    train, evalset = _split_train_eval(rows, train_frac=0.7)

    train_p = np.asarray([r["model_fair_prob"] for r in train], dtype=float)
    train_y = _outcome_vec(train)
    eval_p = np.asarray([r["model_fair_prob"] for r in evalset], dtype=float)
    eval_y = _outcome_vec(evalset)

    platt = PlattCalibrator.fit(train_p, train_y)
    iso = IsotonicCalibrator.fit(train_p, train_y)

    raw_eval_metrics = _summary_metrics(eval_p, eval_y)
    platt_eval_metrics = _summary_metrics(platt.predict(eval_p), eval_y)
    iso_eval_metrics = _summary_metrics(iso.predict(eval_p), eval_y)

    all_p = np.concatenate([train_p, eval_p])
    all_y = np.concatenate([train_y, eval_y])
    raw_all = _summary_metrics(all_p, all_y)
    platt_all_probs = platt.predict(all_p)
    iso_all_probs = iso.predict(all_p)
    platt_all = _summary_metrics(platt_all_probs, all_y)
    iso_all = _summary_metrics(iso_all_probs, all_y)

    raw_reliability = _reliability_table(all_p, all_y, n_bins=10)
    platt_reliability = _reliability_table(platt_all_probs, all_y, n_bins=10)
    iso_reliability = _reliability_table(iso_all_probs, all_y, n_bins=10)

    all_rows = train + evalset
    raw_clv = _clv_analysis(all_rows, all_p)
    platt_clv = _clv_analysis(all_rows, platt_all_probs)
    iso_clv = _clv_analysis(all_rows, iso_all_probs)

    source_breakdown = _hit_rate_by_source_class(all_rows)

    if platt_eval_metrics["brier"] is None and iso_eval_metrics["brier"] is None:
        chosen = "identity"
    elif iso_eval_metrics["brier"] is not None and (
        platt_eval_metrics["brier"] is None or iso_eval_metrics["brier"] <= platt_eval_metrics["brier"]
    ):
        chosen = "isotonic"
    else:
        chosen = "platt"

    chosen_improvement = None
    if raw_eval_metrics["brier"] is not None:
        ref_brier = (
            iso_eval_metrics["brier"] if chosen == "isotonic"
            else platt_eval_metrics["brier"] if chosen == "platt"
            else raw_eval_metrics["brier"]
        )
        chosen_improvement = round(raw_eval_metrics["brier"] - ref_brier, 6)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": db_path,
        "total_events": len(rows),
        "train_events": len(train),
        "eval_events": len(evalset),
        "closing_line_matches": clv_hit,
        "raw_metrics_eval": raw_eval_metrics,
        "platt_metrics_eval": platt_eval_metrics,
        "isotonic_metrics_eval": iso_eval_metrics,
        "raw_metrics_all": raw_all,
        "platt_metrics_all": platt_all,
        "isotonic_metrics_all": iso_all,
        "raw_reliability": raw_reliability,
        "platt_reliability": platt_reliability,
        "isotonic_reliability": iso_reliability,
        "clv_check_raw": raw_clv,
        "clv_check_platt": platt_clv,
        "clv_check_isotonic": iso_clv,
        "source_class_hit_rate": source_breakdown,
        "platt_params": platt.to_dict(),
        "isotonic_params": {
            "kind": "isotonic",
            "n_points": len(iso.x),
            "n_train": iso.n_train,
        },
        "chosen_calibrator": chosen,
        "chosen_brier_improvement": chosen_improvement,
        "installed": False,
    }

    if install:
        if chosen == "platt":
            cal = platt
        elif chosen == "isotonic":
            cal = iso
        else:
            cal = IdentityCalibrator()
        meta = {
            "generated_at": report["generated_at"],
            "source": "backtest_events",
            "n_train": len(train),
            "n_eval": len(evalset),
            "eval_brier_raw": raw_eval_metrics["brier"],
            "eval_brier_calibrated": (
                iso_eval_metrics["brier"] if chosen == "isotonic"
                else platt_eval_metrics["brier"] if chosen == "platt"
                else raw_eval_metrics["brier"]
            ),
            "eval_ece_raw": raw_eval_metrics["ece"],
            "eval_ece_calibrated": (
                iso_eval_metrics["ece"] if chosen == "isotonic"
                else platt_eval_metrics["ece"] if chosen == "platt"
                else raw_eval_metrics["ece"]
            ),
            "clv_trustworthy": raw_clv.get("trustworthy"),
        }
        saved = save_calibrator(cal, metadata=meta)
        report["installed"] = True
        report["calibrator_path"] = saved

    return report


def _print_summary(report: dict) -> None:
    print("Edge Confidence Calibration Report")
    print("=" * 60)
    print(f"  generated_at         : {report['generated_at']}")
    print(f"  total_events         : {report['total_events']}")
    print(f"  train / eval split   : {report['train_events']} / {report['eval_events']}")
    print(f"  closing_line_matches : {report['closing_line_matches']}")
    print()
    print("  EVAL split metrics:")
    for key, label in [
        ("raw_metrics_eval", "raw"),
        ("platt_metrics_eval", "platt"),
        ("isotonic_metrics_eval", "isotonic"),
    ]:
        m = report[key]
        print(f"    {label:<10} n={m['n']}  Brier={m['brier']}  ECE={m['ece']}  "
              f"mean_pred={m['mean_pred']}  hit_rate={m['hit_rate']}")
    print()
    print("  CLV sanity check (mean CLV > 0 => calibration trustworthy):")
    for key, label in [("clv_check_raw", "raw"), ("clv_check_platt", "platt"),
                       ("clv_check_isotonic", "isotonic")]:
        c = report[key]
        print(f"    {label:<10} n={c['n']}  mean_clv={c['mean_clv']}  "
              f"pearson={c['pearson_cal_vs_clv']}  trustworthy={c['trustworthy']}")
    print()
    print(f"  Chosen calibrator    : {report['chosen_calibrator']}")
    print(f"  Eval Brier gain      : {report['chosen_brier_improvement']}")
    if report.get("installed"):
        print(f"  Installed to         : {report.get('calibrator_path')}")
    else:
        print("  Installed            : NO (pass --install to persist)")
    print()
    print(_ascii_reliability_diagram(report["raw_reliability"], title="RAW reliability"))
    print()
    print(_ascii_reliability_diagram(
        report["platt_reliability"] if report["chosen_calibrator"] == "platt"
        else report["isotonic_reliability"],
        title=f"CALIBRATED ({report['chosen_calibrator']}) reliability",
    ))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Edge confidence calibration report.")
    ap.add_argument("--db", default=None, help="Path to callisto.db (read-only).")
    ap.add_argument("--install", action="store_true",
                    help="Save chosen calibrator to memory/edge_calibrator.json.")
    ap.add_argument("--min-rows", type=int, default=200,
                    help="Minimum resolvable events required to generate a report.")
    ap.add_argument("--out-dir", default=None,
                    help="Directory to write JSON report (default: reports/).")
    ap.add_argument("--quiet", action="store_true", help="Suppress stdout summary.")
    args = ap.parse_args(argv)

    db_path = _resolve_db_path(args.db)
    try:
        report = build_report(db_path, min_rows=args.min_rows, install=args.install)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else (ROOT / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    out_path = out_dir / f"edge_calibration_{today}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    if not args.quiet:
        _print_summary(report)
        print(f"\n  JSON written to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
