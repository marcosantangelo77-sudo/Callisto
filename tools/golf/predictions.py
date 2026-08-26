"""2026 pre-tournament predictions and composite scoring."""

import json
import logging
import sqlite3

logger = logging.getLogger("callisto.golf_masters")

from tools.golf.db import DB_PATH, ensure_masters_schema
from tools.golf.backtest import _compute_masters_fit_score_for_player

# ──────────────────────────────────────────────────
# 2026 PREDICTIONS
# ──────────────────────────────────────────────────

def generate_2026_predictions(
    hypothesis_id: str,
    hypothesis_config: dict,
    db_path: str = DB_PATH,
) -> dict:
    """
    Generate 2026 Masters predictions by combining:
    1. Full historical Masters data (2010-2025) for course-fit modeling
    2. Current 2026 season stats (if available) for form adjustment
    3. Hypothesis-specific signals

    Returns ranked list with predicted finish ranges and probabilities.
    """
    ensure_masters_schema(db_path)
    conn = sqlite3.connect(db_path)

    # Load all historical data
    all_years = list(range(2010, 2026))
    all_historical = {}
    for year in all_years:
        rows = conn.execute(
            "SELECT * FROM masters_historical WHERE year = ?", (year,)
        ).fetchall()
        if rows:
            cols = [desc[0] for desc in conn.execute(
                "SELECT * FROM masters_historical LIMIT 0"
            ).description]
            all_historical[year] = [dict(zip(cols, row)) for row in rows]

    # Load expected 2026 field
    field_rows = conn.execute(
        "SELECT player, qualification_category FROM masters_field WHERE year = 2026"
    ).fetchall()
    if not field_rows:
        # Fall back to players who have appeared in recent Masters
        recent_players = set()
        for year in range(2020, 2026):
            rows = conn.execute(
                "SELECT DISTINCT player FROM masters_historical WHERE year = ?", (year,)
            ).fetchall()
            for row in rows:
                recent_players.add(row[0])
        field_players = [(p, "historical") for p in recent_players]
    else:
        field_players = [(row[0], row[1]) for row in field_rows]

    # Load current season stats
    season_stats = {}
    stats_rows = conn.execute(
        "SELECT * FROM pga_season_stats WHERE year = 2026"
    ).fetchall()
    if stats_rows:
        cols = [desc[0] for desc in conn.execute(
            "SELECT * FROM pga_season_stats LIMIT 0"
        ).description]
        for row in stats_rows:
            entry = dict(zip(cols, row))
            season_stats[entry["player"]] = entry

    predictions = []
    train_years = list(all_historical.keys())

    for player, category in field_players:
        # Base score from historical Masters performance
        base_score = _compute_masters_fit_score_for_player(
            player, train_years, all_historical, hypothesis_config
        )

        # Adjust for current form (if season stats available)
        form_adjustment = 0.0
        if player in season_stats:
            stats = season_stats[player]
            sg_total = stats.get("sg_total")
            if sg_total is not None:
                form_adjustment = sg_total * 5  # +5 points per SG:Total

        final_score = min(100, max(0, base_score + form_adjustment))

        predictions.append({
            "player": player,
            "category": category,
            "masters_fit_score": round(final_score, 1),
        })

    # Sort by score
    predictions.sort(key=lambda p: p["masters_fit_score"], reverse=True)

    # Assign predicted ranks and probabilities
    # Use historical base rates for probability calibration
    # Top-10 rate: ~50 players compete, 10 finish in top-10 = 20% base rate
    # Winner: 1/50 = 2% base rate
    # Adjust based on relative score

    total_score = sum(p["masters_fit_score"] for p in predictions)
    if total_score == 0:
        total_score = 1

    for i, pred in enumerate(predictions):
        rank = i + 1
        score_share = pred["masters_fit_score"] / total_score

        # Probability estimates calibrated to field size
        n_field = len(predictions)
        base_win = 1 / max(n_field, 1)
        base_top5 = 5 / max(n_field, 1)
        base_top10 = 10 / max(n_field, 1)
        base_top20 = 20 / max(n_field, 1)
        base_cut = 0.55  # ~55% of field makes cut

        # Scale probabilities by relative score
        score_multiplier = score_share * n_field  # 1.0 = average player
        score_multiplier = max(0.1, min(5.0, score_multiplier))

        pred["predicted_rank"] = rank
        pred["win_prob"] = round(min(0.35, base_win * score_multiplier * 1.5), 4)
        pred["top5_prob"] = round(min(0.60, base_top5 * score_multiplier * 1.3), 4)
        pred["top10_prob"] = round(min(0.75, base_top10 * score_multiplier * 1.2), 4)
        pred["top20_prob"] = round(min(0.85, base_top20 * score_multiplier * 1.1), 4)
        pred["cut_prob"] = round(min(0.95, base_cut * min(score_multiplier, 2.0)), 4)

        # Confidence interval for predicted finish
        # Wider for players with less history
        history_years = sum(
            1 for y in train_years
            if any(e["player"] == pred["player"] for e in all_historical.get(y, []))
        )
        width = max(5, 30 - history_years * 2)
        pred["confidence_low"] = max(1, rank - width // 2)
        pred["confidence_high"] = min(n_field, rank + width // 2)
        pred["masters_experience"] = history_years

    # Store predictions
    for pred in predictions:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO masters_predictions "
                "(hypothesis_id, year, player, masters_fit_score, predicted_rank, "
                "top5_prob, top10_prob, top20_prob, cut_prob, win_prob, "
                "confidence_low, confidence_high, key_factors) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (hypothesis_id, 2026, pred["player"], pred["masters_fit_score"],
                 pred["predicted_rank"], pred["top5_prob"], pred["top10_prob"],
                 pred["top20_prob"], pred["cut_prob"], pred["win_prob"],
                 pred["confidence_low"], pred["confidence_high"],
                 json.dumps({"category": pred["category"],
                            "experience": pred["masters_experience"]}))
            )
        except Exception as e:
            logger.warning(f"Failed to store prediction for {pred['player']}: {e}")

    conn.commit()
    conn.close()

    return {
        "hypothesis_id": hypothesis_id,
        "year": 2026,
        "field_size": len(predictions),
        "predictions": predictions,
    }


# ──────────────────────────────────────────────────
# COMPOSITE SCORING
# ──────────────────────────────────────────────────

def compute_masters_fit_score(
    player: str,
    year: int = 2026,
    db_path: str = DB_PATH,
) -> dict:
    """
    Compute composite Masters fit score combining all hypothesis predictions.

    Aggregates across all Masters hypotheses with their backtest performance
    as weights — hypotheses that backtest better get more influence.

    Returns score 0-100, rank within field, and contributing factors.
    """
    conn = sqlite3.connect(db_path)

    # Get all Masters hypothesis IDs
    hypo_rows = conn.execute(
        "SELECT hypothesis_id, name FROM hypotheses "
        "WHERE (sport = 'golf_pga_masters' OR name LIKE '%Masters%') "
        "AND status != 'rejected'"
    ).fetchall()

    if not hypo_rows:
        conn.close()
        return {"error": "No Masters hypotheses found"}

    # Get backtest performance for each hypothesis (as weight)
    hypothesis_weights = {}
    for hid, name in hypo_rows:
        bt_rows = conn.execute(
            "SELECT AVG(rank_correlation) as avg_corr, COUNT(*) as n_folds "
            "FROM masters_backtest_results WHERE hypothesis_id = ?",
            (hid,)
        ).fetchone()
        if bt_rows and bt_rows[0] is not None:
            # Weight by correlation strength (min 0.1 to avoid zero weights)
            hypothesis_weights[hid] = max(0.1, bt_rows[0])
        else:
            hypothesis_weights[hid] = 0.5  # default weight for untested

    # Get predictions for this player across all hypotheses
    scores = []
    for hid, name in hypo_rows:
        pred = conn.execute(
            "SELECT masters_fit_score FROM masters_predictions "
            "WHERE hypothesis_id = ? AND year = ? AND player = ?",
            (hid, year, player)
        ).fetchone()
        if pred and pred[0] is not None:
            weight = hypothesis_weights.get(hid, 0.5)
            scores.append((pred[0], weight, name))

    conn.close()

    if not scores:
        return {
            "player": player,
            "composite_score": None,
            "error": "No predictions found for this player"
        }

    # Weighted average
    total_weight = sum(w for _, w, _ in scores)
    composite = sum(s * w for s, w, _ in scores) / total_weight if total_weight > 0 else 0

    return {
        "player": player,
        "year": year,
        "composite_score": round(composite, 1),
        "n_hypotheses": len(scores),
        "contributing_hypotheses": [
            {"name": name, "score": round(s, 1), "weight": round(w, 3)}
            for s, w, name in sorted(scores, key=lambda x: x[1], reverse=True)[:10]
        ],
    }
