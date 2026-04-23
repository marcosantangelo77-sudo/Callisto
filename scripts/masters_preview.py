"""
Masters 2026 Pre-Tournament Preview Generator

Loads historical Masters data, applies each hypothesis using LOO methodology,
and generates a 2026 Masters preview with:
  - Player rankings by composite Masters fit score
  - Top-10, top-20, and cut probabilities
  - Recommended bets
  - Hypothesis validation results

Usage:
    python scripts/masters_preview.py

Output:
    Prints formatted preview + saves JSON to memory/masters_2026_preview.json
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.golf_masters import (
    ensure_masters_schema,
    fetch_masters_historical,
    fetch_masters_field,
    fetch_current_season_stats,
    leave_one_out_backtest,
    rolling_window_backtest,
    generate_2026_predictions,
    compute_masters_fit_score,
)

DB_PATH = "memory/callisto.db"


def print_header(text: str) -> None:
    width = 80
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)


def print_section(text: str) -> None:
    print(f"\n--- {text} ---")


def run():
    print_header("MASTERS 2026 PRE-TOURNAMENT PREVIEW")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Event: The Masters Tournament, Augusta National Golf Club")
    print(f"Dates: April 10-13, 2026")

    # ── STEP 1: Ensure data ──
    print_section("DATA COLLECTION")

    ensure_masters_schema(DB_PATH)

    hist_result = fetch_masters_historical(db_path=DB_PATH)
    print(f"Historical data: {hist_result['years_fetched']} years fetched, "
          f"{hist_result['years_cached']} cached, "
          f"{hist_result['total_players']} total player-records")

    field_result = fetch_masters_field(2026, DB_PATH)
    print(f"2026 Field: {field_result['players']} players ({field_result['status']})")

    season_result = fetch_current_season_stats(2026, DB_PATH)
    print(f"2026 Season stats: {season_result['players']} players ({season_result['status']})")

    # ── STEP 2: Load hypotheses ──
    print_section("HYPOTHESIS TESTING")

    conn = sqlite3.connect(DB_PATH)
    hypo_rows = conn.execute(
        "SELECT hypothesis_id, name, thesis, model_config, market_type "
        "FROM hypotheses "
        "WHERE (sport = 'golf_pga_masters' OR (sport = 'golf_pga' AND name LIKE '%Masters%')) "
        "AND status != 'rejected' "
        "ORDER BY name"
    ).fetchall()
    print(f"Masters hypotheses to test: {len(hypo_rows)}")

    # ── STEP 3: Run LOO backtests ──
    hypothesis_results = []
    loo_summary = []

    for hid, name, thesis, config_str, market_type in hypo_rows:
        config = json.loads(config_str) if config_str else {}
        print(f"\n  Testing: {name}")

        # Leave-one-out backtest
        loo_result = leave_one_out_backtest(hid, config, db_path=DB_PATH)

        if "error" in loo_result:
            print(f"    LOO Error: {loo_result['error']}")
            continue

        avg_corr = loo_result.get("avg_rank_correlation", 0)
        avg_t10 = loo_result.get("avg_top10_accuracy", 0)
        avg_t10r = loo_result.get("avg_top10_recall", 0)
        winner_rate = loo_result.get("winner_in_top10_rate", 0)
        n_folds = loo_result.get("n_folds", 0)

        # Determine hypothesis quality grade
        if avg_corr > 0.3 and avg_t10 > 0.25:
            grade = "A"
        elif avg_corr > 0.15 and avg_t10 > 0.15:
            grade = "B"
        elif avg_corr > 0.05:
            grade = "C"
        else:
            grade = "D"

        print(f"    LOO ({n_folds} folds): "
              f"RankCorr={avg_corr:.3f} | "
              f"Top10Acc={avg_t10:.1%} | "
              f"Top10Recall={avg_t10r:.1%} | "
              f"WinnerInTop10={winner_rate:.1%} | "
              f"Grade={grade}")

        # Also run rolling window
        rw_result = rolling_window_backtest(hid, config, train_window=5, db_path=DB_PATH)
        rw_corr = rw_result.get("avg_rank_correlation", 0) if "error" not in rw_result else 0

        hypothesis_results.append({
            "hypothesis_id": hid,
            "name": name,
            "market_type": market_type,
            "grade": grade,
            "loo_rank_correlation": avg_corr,
            "loo_top10_accuracy": avg_t10,
            "loo_top10_recall": avg_t10r,
            "loo_winner_in_top10": winner_rate,
            "rw_rank_correlation": rw_corr,
            "n_folds": n_folds,
        })

        loo_summary.append({
            "name": name, "grade": grade, "corr": avg_corr, "t10": avg_t10,
        })

    conn.close()

    # ── STEP 4: Generate 2026 predictions ──
    print_section("2026 PREDICTIONS")

    # Generate predictions from each hypothesis
    all_predictions = {}
    for hr in hypothesis_results:
        conn = sqlite3.connect(DB_PATH)
        config_row = conn.execute(
            "SELECT model_config FROM hypotheses WHERE hypothesis_id = ?",
            (hr["hypothesis_id"],)
        ).fetchone()
        conn.close()

        if not config_row:
            continue
        config = json.loads(config_row[0]) if config_row[0] else {}

        pred_result = generate_2026_predictions(
            hr["hypothesis_id"], config, DB_PATH
        )

        if "predictions" in pred_result:
            for pred in pred_result["predictions"]:
                player = pred["player"]
                if player not in all_predictions:
                    all_predictions[player] = {
                        "scores": [],
                        "weights": [],
                        "top10_probs": [],
                        "win_probs": [],
                        "cut_probs": [],
                    }
                weight = max(0.1, hr["loo_rank_correlation"])
                all_predictions[player]["scores"].append(pred["masters_fit_score"])
                all_predictions[player]["weights"].append(weight)
                all_predictions[player]["top10_probs"].append(pred.get("top10_prob", 0))
                all_predictions[player]["win_probs"].append(pred.get("win_prob", 0))
                all_predictions[player]["cut_probs"].append(pred.get("cut_prob", 0))

    # Compute composite scores
    composite_rankings = []
    for player, data in all_predictions.items():
        total_weight = sum(data["weights"])
        if total_weight == 0:
            continue
        composite_score = sum(s * w for s, w in zip(data["scores"], data["weights"])) / total_weight
        avg_top10 = sum(p * w for p, w in zip(data["top10_probs"], data["weights"])) / total_weight
        avg_win = sum(p * w for p, w in zip(data["win_probs"], data["weights"])) / total_weight
        avg_cut = sum(p * w for p, w in zip(data["cut_probs"], data["weights"])) / total_weight
        n_hypotheses = len(data["scores"])

        composite_rankings.append({
            "player": player,
            "composite_score": round(composite_score, 1),
            "top10_prob": round(avg_top10, 4),
            "win_prob": round(avg_win, 4),
            "cut_prob": round(avg_cut, 4),
            "n_hypotheses": n_hypotheses,
        })

    composite_rankings.sort(key=lambda x: x["composite_score"], reverse=True)

    # ── STEP 5: Print preview ──
    print_header("COMPOSITE PLAYER RANKINGS — MASTERS 2026")
    print(f"{'Rank':>4s}  {'Player':<28s}  {'Score':>6s}  {'Win%':>6s}  {'Top10%':>7s}  {'Cut%':>6s}  {'Hypos':>5s}")
    print("-" * 80)

    for i, p in enumerate(composite_rankings[:40]):
        rank = i + 1
        print(f"{rank:4d}  {p['player']:<28s}  {p['composite_score']:6.1f}  "
              f"{p['win_prob']*100:5.1f}%  {p['top10_prob']*100:6.1f}%  "
              f"{p['cut_prob']*100:5.1f}%  {p['n_hypotheses']:5d}")

    # ── STEP 6: Bet recommendations ──
    print_header("BET RECOMMENDATIONS — MASTERS 2026")

    # Top-10 value plays: high composite score players not in the top-5 favorites
    print_section("TOP-10 VALUE PLAYS (ranked 6-20 by composite, high top-10 probability)")
    value_plays = composite_rankings[5:20]
    for p in value_plays:
        if p["top10_prob"] > 0.08:
            print(f"  {p['player']:<28s}  Score: {p['composite_score']:5.1f}  "
                  f"Top10: {p['top10_prob']*100:5.1f}%")

    print_section("OUTRIGHT LONGSHOTS (ranked 15-30, above-average win probability)")
    longshots = composite_rankings[14:30]
    for p in longshots:
        if p["win_prob"] > 0.01:
            print(f"  {p['player']:<28s}  Score: {p['composite_score']:5.1f}  "
                  f"Win: {p['win_prob']*100:5.2f}%")

    print_section("MAKE CUT FADES (lowest cut probability in field)")
    fade_candidates = sorted(composite_rankings, key=lambda x: x["cut_prob"])
    for p in fade_candidates[:10]:
        print(f"  {p['player']:<28s}  Score: {p['composite_score']:5.1f}  "
              f"Cut: {p['cut_prob']*100:5.1f}%")

    # ── STEP 7: Hypothesis leaderboard ──
    print_header("HYPOTHESIS PERFORMANCE LEADERBOARD")
    hypothesis_results.sort(key=lambda x: x["loo_rank_correlation"], reverse=True)

    print(f"{'Grade':>5s}  {'RankCorr':>8s}  {'Top10Acc':>8s}  {'Recall':>8s}  {'Name':<50s}")
    print("-" * 80)
    for hr in hypothesis_results[:20]:
        print(f"{hr['grade']:>5s}  {hr['loo_rank_correlation']:8.3f}  "
              f"{hr['loo_top10_accuracy']:7.1%}  {hr['loo_top10_recall']:7.1%}  "
              f"{hr['name'][:50]:<50s}")

    # Grade distribution
    grades = {}
    for hr in hypothesis_results:
        g = hr["grade"]
        grades[g] = grades.get(g, 0) + 1
    print(f"\nGrade distribution: " + ", ".join(f"{g}={n}" for g, n in sorted(grades.items())))

    # ── STEP 8: Save JSON ──
    preview = {
        "generated_at": datetime.now().isoformat(),
        "event": "Masters 2026",
        "venue": "Augusta National",
        "dates": "April 10-13, 2026",
        "rankings": composite_rankings,
        "hypothesis_results": hypothesis_results,
        "data_summary": {
            "historical_years": list(range(2010, 2026)),
            "field_size": len(composite_rankings),
            "hypotheses_tested": len(hypothesis_results),
        },
    }

    output_path = Path("memory/masters_2026_preview.json")
    output_path.write_text(json.dumps(preview, indent=2, default=str))
    print(f"\nFull preview saved to: {output_path.resolve()}")

    # ── STEP 9: Post-tournament validation stub ──
    print_section("POST-TOURNAMENT VALIDATION (run after April 13)")
    print("After the Masters concludes, run:")
    print("  python -c \"from tools.golf_masters import fetch_masters_historical; "
          "fetch_masters_historical(range(2026, 2027))\"")
    print("Then re-run this script to compare predictions vs actuals.")

    return preview


if __name__ == "__main__":
    preview = run()
    print(f"\nDone. {len(preview['rankings'])} players ranked, "
          f"{len(preview['hypothesis_results'])} hypotheses tested.")
