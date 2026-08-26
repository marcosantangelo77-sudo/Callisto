"""Leave-one-out and rolling-window backtesting for Masters hypotheses."""

import json
import logging
import math
import sqlite3

logger = logging.getLogger("callisto.golf_masters")

from tools.golf.db import DB_PATH, ensure_masters_schema

# ──────────────────────────────────────────────────
# LEAVE-ONE-OUT CROSS-VALIDATION BACKTEST
# ──────────────────────────────────────────────────

def _spearman_rank_correlation(predicted: list[tuple[str, float]], actual: list[tuple[str, int]]) -> float:
    """
    Compute Spearman rank correlation between predicted scores and actual finishes.
    Only considers players present in both lists.
    """
    # Build dictionaries
    pred_dict = {name: score for name, score in predicted}
    act_dict = {name: pos for name, pos in actual}

    # Find common players
    common = [p for p in pred_dict if p in act_dict]
    if len(common) < 3:
        return 0.0

    # Rank predictions (higher score = better predicted rank)
    pred_sorted = sorted(common, key=lambda p: pred_dict[p], reverse=True)
    pred_ranks = {p: i + 1 for i, p in enumerate(pred_sorted)}

    # Rank actuals (lower position = better actual rank)
    act_sorted = sorted(common, key=lambda p: act_dict[p])
    act_ranks = {p: i + 1 for i, p in enumerate(act_sorted)}

    n = len(common)
    d_squared_sum = sum((pred_ranks[p] - act_ranks[p]) ** 2 for p in common)

    # Spearman formula: 1 - (6 * sum(d^2)) / (n * (n^2 - 1))
    if n * (n ** 2 - 1) == 0:
        return 0.0
    return 1 - (6 * d_squared_sum) / (n * (n ** 2 - 1))


def _compute_masters_fit_score_for_player(
    player: str,
    train_years: list[int],
    all_historical: dict,
    hypothesis_config: dict,
) -> float:
    """
    Compute a Masters fit score for a player based on training data and hypothesis config.

    This is the core prediction function. It combines:
    1. Augusta course history (past finishes, weighted by recency)
    2. Hypothesis-specific signals (based on model_config type)

    Returns a score where higher = better predicted performance.
    """
    hypothesis_type = hypothesis_config.get("type", "general")
    score = 50.0  # baseline

    # Gather player's historical performance at the Masters
    player_history = []
    for y in train_years:
        if y in all_historical:
            for entry in all_historical[y]:
                if entry["player"] == player:
                    player_history.append(entry)

    # ── COMPONENT 1: Augusta Course History (40% weight) ──
    course_history_score = 0.0
    if player_history:
        # Recency-weighted average finish
        weighted_sum = 0.0
        weight_total = 0.0
        for entry in player_history:
            pos = entry.get("position_numeric", 50)
            if pos >= 997:  # WD/DQ
                pos = 60
            elif pos >= 999:  # CUT
                pos = 50
            year = entry["year"]
            # Exponential decay: recent years matter more
            recency_weight = 0.85 ** (max(train_years) - year)
            # Convert position to score (1st = 100, 50th = 0)
            pos_score = max(0, 100 - (pos - 1) * 2)
            weighted_sum += pos_score * recency_weight
            weight_total += recency_weight

        if weight_total > 0:
            course_history_score = weighted_sum / weight_total

        # Bonus for multiple top-10 finishes
        top10_count = sum(1 for e in player_history if e.get("position_numeric", 99) <= 10)
        if top10_count >= 3:
            course_history_score += 10
        elif top10_count >= 2:
            course_history_score += 5

        # Bonus for making cuts consistently
        cuts_made = sum(1 for e in player_history if e.get("cut_made"))
        if len(player_history) > 0:
            cut_rate = cuts_made / len(player_history)
            if cut_rate > 0.8:
                course_history_score += 5
    else:
        # First-timer penalty
        course_history_score = 30  # neutral-low for unknowns

    # ── COMPONENT 2: Hypothesis-Specific Signal (40% weight) ──
    hypothesis_signal = 50.0  # neutral baseline
    hypothesis_name = hypothesis_config.get("name", "")

    if hypothesis_type == "strokes_gained_decomposition":
        # SG:Approach / SG:Around Green / SG:Tee-to-Green hypotheses
        # Use scoring as proxy for SG when actual SG data unavailable
        key_stat = hypothesis_config.get("key_stat", "sg_approach")
        sg_found = False
        for entry in player_history:
            sg_val = entry.get(key_stat)
            if sg_val is not None:
                hypothesis_signal = 50 + sg_val * 20
                sg_found = True
        if not sg_found and player_history:
            # Proxy: use scoring relative to field average as SG estimate
            for entry in sorted(player_history, key=lambda e: e["year"], reverse=True):
                total_to_par = entry.get("total_to_par")
                if total_to_par is not None:
                    # Weight differently based on which SG component
                    if "approach" in key_stat:
                        hypothesis_signal = 50 + (-total_to_par) * 2.5  # approach = 2nd shot dominance
                    elif "around_green" in key_stat:
                        hypothesis_signal = 50 + (-total_to_par) * 2.0  # short game
                    elif "putting" in key_stat:
                        hypothesis_signal = 50 + (-total_to_par) * 1.8  # putting on bentgrass
                    elif "tee_to_green" in key_stat:
                        hypothesis_signal = 50 + (-total_to_par) * 2.8  # ball-striking
                    else:
                        hypothesis_signal = 50 + (-total_to_par) * 2.0
                    break

    elif hypothesis_type == "scoring_distribution":
        # Par-5 scoring — weight total scoring and low rounds
        if player_history:
            # Use best-ever round as par-5 proxy (par-5 eagles drive low rounds)
            best_rounds = []
            for entry in player_history:
                for r in [entry.get("r1"), entry.get("r2"), entry.get("r3"), entry.get("r4")]:
                    if r and r > 0:
                        best_rounds.append(r)
            if best_rounds:
                best_round = min(best_rounds)
                avg_round = sum(best_rounds) / len(best_rounds)
                # Low single-round scores indicate par-5 birdie/eagle ability
                hypothesis_signal = 50 + (72 - best_round) * 4 + (72 - avg_round) * 1.5

    elif hypothesis_type in ("course_horse", "specialist_repeat"):
        # Course horse — heavily weight prior Augusta results
        if player_history:
            best_finish = min(e.get("position_numeric", 99) for e in player_history)
            appearances = len(player_history)
            hypothesis_signal = 50 + (50 - best_finish) * 1.5 + appearances * 3

    elif hypothesis_type == "first_timer_fade":
        # Fade first-timers — experience matters at Augusta
        direction = hypothesis_config.get("direction", "fade")
        if not player_history:
            hypothesis_signal = 20 if direction == "fade" else 75
        else:
            # More appearances = more signal, with diminishing returns
            apps = len(player_history)
            hypothesis_signal = 55 + min(apps, 10) * 3.5

    elif hypothesis_type in ("age_discount", "veteran_fade"):
        # Age-based hypothesis — proxy via career timeline
        if player_history:
            latest = max(player_history, key=lambda e: e["year"])
            earliest = min(player_history, key=lambda e: e["year"])
            career_span = latest["year"] - earliest["year"]
            age = latest.get("age")
            if age and age > 42:
                hypothesis_signal = max(15, 50 - (age - 40) * 6)
            elif age and age < 28:
                hypothesis_signal = 65
            elif career_span > 12:
                hypothesis_signal = max(20, 55 - career_span * 2)
            else:
                hypothesis_signal = 55
        else:
            hypothesis_signal = 60  # unknowns assumed younger

    elif hypothesis_type == "hole_level_analysis":
        # Amen Corner / specific hole performance
        # Without hole-level data, use weekend scoring as proxy (Amen Corner
        # determines weekend survival and contention)
        if player_history:
            weekend_scores = []
            for entry in player_history:
                r3, r4 = entry.get("r3"), entry.get("r4")
                if r3 and r4 and r3 > 0 and r4 > 0:
                    weekend_scores.append(r3 + r4)
            if weekend_scores:
                avg_weekend = sum(weekend_scores) / len(weekend_scores)
                # Lower weekend scoring = better Amen Corner performance
                hypothesis_signal = 50 + (144 - avg_weekend) * 2.5

    elif hypothesis_type == "recent_form":
        # Recent form weighting — heavy recency bias
        if player_history:
            sorted_history = sorted(player_history, key=lambda e: e["year"], reverse=True)
            recent = sorted_history[0]
            pos = recent.get("position_numeric", 50)
            if pos < 997:
                hypothesis_signal = max(0, 100 - (pos - 1) * 2.5)
            else:
                hypothesis_signal = 20  # recent MC

    elif hypothesis_type == "round_improvement":
        # R1-R4 improvement pattern (Sunday closers)
        if player_history:
            improvements = []
            for entry in player_history:
                r1, r4 = entry.get("r1"), entry.get("r4")
                if r1 and r4 and r1 > 0 and r4 > 0:
                    improvements.append(r1 - r4)  # positive = improved
            if improvements:
                avg_improve = sum(improvements) / len(improvements)
                hypothesis_signal = 50 + avg_improve * 8  # ~8 pts per stroke improvement

    elif hypothesis_type == "narrative":
        # Ryder Cup / motivation-based — use recent results as proxy
        if player_history:
            recent = sorted(player_history, key=lambda e: e["year"], reverse=True)[:3]
            recent_avg = sum(
                e.get("position_numeric", 50) for e in recent if e.get("position_numeric", 999) < 997
            )
            n = sum(1 for e in recent if e.get("position_numeric", 999) < 997)
            if n > 0:
                hypothesis_signal = max(0, 100 - (recent_avg / n - 1) * 2)

    elif hypothesis_type == "weather_impact":
        # Weather-based: bombers in soft conditions, course management in cold
        # Proxy: players with high round-to-round variance adapt to conditions
        if player_history:
            all_rounds = []
            for entry in player_history:
                for r in [entry.get("r1"), entry.get("r2"), entry.get("r3"), entry.get("r4")]:
                    if r and r > 0:
                        all_rounds.append(r)
            if len(all_rounds) >= 4:
                avg = sum(all_rounds) / len(all_rounds)
                variance = sum((r - avg) ** 2 for r in all_rounds) / (len(all_rounds) - 1)
                # Lower variance = more consistent = better in variable weather
                std = math.sqrt(variance)
                hypothesis_signal = max(0, 80 - std * 6)

    else:
        # Generic: use weighted average finish position with recency decay
        if player_history:
            weighted_pos = 0.0
            total_w = 0.0
            max_year = max(e["year"] for e in player_history)
            for entry in player_history:
                pos = entry.get("position_numeric", 50)
                if pos >= 997:
                    pos = 55  # MC/WD penalty
                recency = 0.80 ** (max_year - entry["year"])
                weighted_pos += pos * recency
                total_w += recency
            if total_w > 0:
                avg_pos = weighted_pos / total_w
                hypothesis_signal = max(0, 100 - avg_pos * 1.8)

    # ── COMPONENT 3: Consistency / Cut-Making (20% weight) ──
    consistency_score = 50.0
    if player_history:
        # Scoring consistency (lower std dev of total_to_par = more consistent)
        totals = [e.get("total_to_par", 0) for e in player_history if e.get("total_to_par") is not None]
        if len(totals) >= 2:
            mean_total = sum(totals) / len(totals)
            variance = sum((t - mean_total) ** 2 for t in totals) / (len(totals) - 1)
            std_dev = math.sqrt(variance)
            # Lower variance = more consistent = higher score
            consistency_score = max(0, 80 - std_dev * 8)

    # ── COMPOSITE SCORE ──
    final_score = (
        course_history_score * 0.40 +
        hypothesis_signal * 0.40 +
        consistency_score * 0.20
    )

    return min(100, max(0, final_score))


def leave_one_out_backtest(
    hypothesis_id: str,
    hypothesis_config: dict,
    years: range = range(2010, 2026),
    db_path: str = DB_PATH,
) -> dict:
    """
    Leave-one-out cross-validation for a Masters hypothesis.

    For each year Y in the range:
    1. Train on all years except Y
    2. Predict outcomes for year Y
    3. Compare predictions to actual results
    4. Track accuracy metrics

    Returns aggregate results across all folds.
    """
    ensure_masters_schema(db_path)
    conn = sqlite3.connect(db_path)

    # Load all historical data
    all_historical = {}
    for year in years:
        rows = conn.execute(
            "SELECT * FROM masters_historical WHERE year = ?", (year,)
        ).fetchall()
        cols = [desc[0] for desc in conn.execute(
            "SELECT * FROM masters_historical LIMIT 0"
        ).description]
        all_historical[year] = [dict(zip(cols, row)) for row in rows]

    # Available years (those with data)
    available_years = [y for y in years if all_historical.get(y)]
    if len(available_years) < 3:
        conn.close()
        return {"error": f"Need at least 3 years of data, have {len(available_years)}"}

    fold_results = []

    for test_year in available_years:
        train_years = [y for y in available_years if y != test_year]
        test_data = all_historical[test_year]

        if not test_data:
            continue

        # Get all unique players in the test year
        test_players = [entry["player"] for entry in test_data]

        # Generate predictions for each test player
        predictions = []
        for player in test_players:
            score = _compute_masters_fit_score_for_player(
                player, train_years, all_historical, hypothesis_config
            )
            predictions.append((player, score))

        # Sort predictions by score (highest = best predicted finish)
        predictions.sort(key=lambda x: x[1], reverse=True)

        # Build actual results
        actuals = [(entry["player"], entry.get("position_numeric", 999)) for entry in test_data]
        actuals_dict = {name: pos for name, pos in actuals}

        # ── METRICS ──

        # Top-10 accuracy: of our predicted top-10, how many actually finished top-10?
        predicted_top10 = set(p[0] for p in predictions[:10])
        actual_top10 = set(name for name, pos in actuals if pos <= 10)
        top10_correct = len(predicted_top10 & actual_top10)
        top10_accuracy = top10_correct / max(len(predicted_top10), 1)
        top10_recall = top10_correct / max(len(actual_top10), 1)

        # Top-20 accuracy
        predicted_top20 = set(p[0] for p in predictions[:20])
        actual_top20 = set(name for name, pos in actuals if pos <= 20)
        top20_correct = len(predicted_top20 & actual_top20)
        top20_accuracy = top20_correct / max(len(predicted_top20), 1)

        # Cut accuracy: did we correctly identify cut-makers?
        predicted_cut_makers = set(p[0] for p in predictions if p[1] > 35)  # threshold
        actual_cut_makers = set(name for name, pos in actuals if pos < 999)
        if predicted_cut_makers:
            cut_accuracy = len(predicted_cut_makers & actual_cut_makers) / len(predicted_cut_makers)
        else:
            cut_accuracy = 0.0

        # Rank correlation
        rank_corr = _spearman_rank_correlation(predictions, actuals)

        # Winner identification
        actual_winner = [name for name, pos in actuals if pos == 1]
        winner_in_top5 = any(name in [p[0] for p in predictions[:5]] for name in actual_winner)
        winner_in_top10 = any(name in [p[0] for p in predictions[:10]] for name in actual_winner)

        fold_result = {
            "test_year": test_year,
            "train_years": train_years,
            "n_players": len(test_players),
            "top10_accuracy": round(top10_accuracy, 4),
            "top10_recall": round(top10_recall, 4),
            "top20_accuracy": round(top20_accuracy, 4),
            "cut_accuracy": round(cut_accuracy, 4),
            "rank_correlation": round(rank_corr, 4),
            "winner_in_top5_pred": winner_in_top5,
            "winner_in_top10_pred": winner_in_top10,
            "predicted_top5": [p[0] for p in predictions[:5]],
            "actual_top5": [name for name, pos in sorted(actuals, key=lambda x: x[1])[:5]],
        }
        fold_results.append(fold_result)

        # Store in database
        try:
            conn.execute(
                "INSERT OR REPLACE INTO masters_backtest_results "
                "(hypothesis_id, method, test_year, train_years, "
                "predictions_json, actuals_json, top10_accuracy, top10_recall, "
                "cut_accuracy, rank_correlation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (hypothesis_id, "leave_one_out", test_year,
                 json.dumps(train_years),
                 json.dumps([(p, round(s, 2)) for p, s in predictions[:20]]),
                 json.dumps([(name, pos) for name, pos in sorted(actuals, key=lambda x: x[1])[:20]]),
                 top10_accuracy, top10_recall, cut_accuracy, rank_corr)
            )
        except Exception as e:
            logger.warning(f"Failed to store LOO result for {test_year}: {e}")

    conn.commit()

    # Aggregate metrics across all folds
    if not fold_results:
        conn.close()
        return {"error": "No valid folds produced"}

    n_folds = len(fold_results)
    agg = {
        "hypothesis_id": hypothesis_id,
        "method": "leave_one_out",
        "n_folds": n_folds,
        "years_tested": [f["test_year"] for f in fold_results],
        "avg_top10_accuracy": round(sum(f["top10_accuracy"] for f in fold_results) / n_folds, 4),
        "avg_top10_recall": round(sum(f["top10_recall"] for f in fold_results) / n_folds, 4),
        "avg_top20_accuracy": round(sum(f["top20_accuracy"] for f in fold_results) / n_folds, 4),
        "avg_cut_accuracy": round(sum(f["cut_accuracy"] for f in fold_results) / n_folds, 4),
        "avg_rank_correlation": round(sum(f["rank_correlation"] for f in fold_results) / n_folds, 4),
        "winner_in_top5_rate": round(sum(1 for f in fold_results if f["winner_in_top5_pred"]) / n_folds, 4),
        "winner_in_top10_rate": round(sum(1 for f in fold_results if f["winner_in_top10_pred"]) / n_folds, 4),
        "fold_details": fold_results,
    }

    conn.close()
    return agg


def rolling_window_backtest(
    hypothesis_id: str,
    hypothesis_config: dict,
    train_window: int = 5,
    years: range = range(2010, 2026),
    db_path: str = DB_PATH,
) -> dict:
    """
    Rolling window backtest: train on N prior years, test on next.

    More realistic than LOO since it simulates what we'd actually know pre-tournament:
    - 2010-2014 -> test 2015
    - 2011-2015 -> test 2016
    - ...
    - 2020-2024 -> test 2025
    """
    ensure_masters_schema(db_path)
    conn = sqlite3.connect(db_path)

    # Load all historical data
    all_historical = {}
    for year in years:
        rows = conn.execute(
            "SELECT * FROM masters_historical WHERE year = ?", (year,)
        ).fetchall()
        cols = [desc[0] for desc in conn.execute(
            "SELECT * FROM masters_historical LIMIT 0"
        ).description]
        all_historical[year] = [dict(zip(cols, row)) for row in rows]

    available_years = sorted(y for y in years if all_historical.get(y))
    if len(available_years) < train_window + 1:
        conn.close()
        return {"error": f"Need at least {train_window + 1} years, have {len(available_years)}"}

    fold_results = []

    for i in range(train_window, len(available_years)):
        test_year = available_years[i]
        train_years = available_years[i - train_window:i]
        test_data = all_historical[test_year]

        if not test_data:
            continue

        test_players = [entry["player"] for entry in test_data]

        predictions = []
        for player in test_players:
            score = _compute_masters_fit_score_for_player(
                player, train_years, all_historical, hypothesis_config
            )
            predictions.append((player, score))

        predictions.sort(key=lambda x: x[1], reverse=True)
        actuals = [(entry["player"], entry.get("position_numeric", 999)) for entry in test_data]

        predicted_top10 = set(p[0] for p in predictions[:10])
        actual_top10 = set(name for name, pos in actuals if pos <= 10)
        top10_correct = len(predicted_top10 & actual_top10)
        top10_accuracy = top10_correct / max(len(predicted_top10), 1)
        top10_recall = top10_correct / max(len(actual_top10), 1)

        rank_corr = _spearman_rank_correlation(predictions, actuals)

        actual_winner = [name for name, pos in actuals if pos == 1]
        winner_in_top10 = any(name in [p[0] for p in predictions[:10]] for name in actual_winner)

        fold_result = {
            "test_year": test_year,
            "train_years": train_years,
            "top10_accuracy": round(top10_accuracy, 4),
            "top10_recall": round(top10_recall, 4),
            "rank_correlation": round(rank_corr, 4),
            "winner_in_top10_pred": winner_in_top10,
            "predicted_top5": [p[0] for p in predictions[:5]],
            "actual_top5": [name for name, pos in sorted(actuals, key=lambda x: x[1])[:5]],
        }
        fold_results.append(fold_result)

        try:
            conn.execute(
                "INSERT OR REPLACE INTO masters_backtest_results "
                "(hypothesis_id, method, test_year, train_years, "
                "predictions_json, actuals_json, top10_accuracy, top10_recall, "
                "rank_correlation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (hypothesis_id, "rolling_window", test_year,
                 json.dumps(train_years),
                 json.dumps([(p, round(s, 2)) for p, s in predictions[:20]]),
                 json.dumps([(name, pos) for name, pos in sorted(actuals, key=lambda x: x[1])[:20]]),
                 top10_accuracy, top10_recall, rank_corr)
            )
        except Exception as e:
            logger.warning(f"Failed to store rolling result for {test_year}: {e}")

    conn.commit()

    if not fold_results:
        conn.close()
        return {"error": "No valid folds"}

    n_folds = len(fold_results)
    agg = {
        "hypothesis_id": hypothesis_id,
        "method": "rolling_window",
        "train_window": train_window,
        "n_folds": n_folds,
        "years_tested": [f["test_year"] for f in fold_results],
        "avg_top10_accuracy": round(sum(f["top10_accuracy"] for f in fold_results) / n_folds, 4),
        "avg_top10_recall": round(sum(f["top10_recall"] for f in fold_results) / n_folds, 4),
        "avg_rank_correlation": round(sum(f["rank_correlation"] for f in fold_results) / n_folds, 4),
        "winner_in_top10_rate": round(sum(1 for f in fold_results if f["winner_in_top10_pred"]) / n_folds, 4),
        "fold_details": fold_results,
    }

    conn.close()
    return agg

