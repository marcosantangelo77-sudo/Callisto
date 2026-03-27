"""Deep Work Cycle #10 cleanup — batch-reject duplicate/losing hypotheses.

Run this when the DB is not locked (API stopped or between backtest runs):
  python scripts/deep_work_10_cleanup.py

Findings:
- 12 NBA spread hypotheses tested IDENTICAL 149 games (game_filters=NULL)
- 3 NBA road-fav hypotheses tested IDENTICAL 59 games
- 1 NHL hypothesis actively losing (19W-25L, 43.2%)
- 2 hypotheses with circular testing (backtest overlaps training)
- 1 hypothesis reset to draft for re-testing with proper filters
"""
import sqlite3
import json
import sys
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "memory", "callisto.db")

REJECT_IDS = [
    # 12 NBA spread hypotheses sharing 149-event set (no game_filters)
    "4c74e1e2-e5c",  # Cross-book consensus divergence on spreads
    "5a522189-399",  # nba_blown_lead_hangover_spread_fade
    "e74bc1e0-1fb",  # nba_closing_line_reversal_steam_fade
    "d6b832cc-bc3",  # nba_closing_line_steam_dog_cover
    "eb97ba1e-689",  # nba_closing_line_value_late_steam
    "8eeebbbe-e43",  # nba_elimination_race_dog_late_season_ats
    "423a5167-30f",  # nba_losing_streak_bounce_ats
    "708b33d1-091",  # nba_loss_streak_favorite_bounce_cover
    "dfd0d7f6-120",  # nba_loss_streak_home_return_ats
    "6f64ad56-f06",  # nba_pacific_early_tip_spread_fade
    "68ea4fa5-8e5",  # nba_sharp_reversal_closing_line_cover
    # 3 NBA road-fav sharing 59-event set
    "2d71b8c8-21a",  # nba_road_favorite_mid_spread_fade
    "2e194e83-c81",  # nba_winning_streak_road_favorite_fade
    "e9b540f3-767",  # nba_road_favorite_3_to_6_ats
    # 1 actively losing
    "be5f7414-edc",  # nhl_goalie_age_workload_interaction_fade (19W-25L)
    # 2 circular testing (backtest_period_start < training_period_end)
    "b124c40b-ba2",  # nba_bubble_team_road_dog_ats
    "86b251b0-5de",  # nba_dog_3_to_7_closing_line_value
]

RESET_TO_DRAFT = [
    "767df495-de1",  # nba_rest_mismatch_3v0_days_visitor_ml (125 signals, worth retesting with filters)
]


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    c = conn.cursor()

    # 1. Batch reject
    ph = ",".join("?" * len(REJECT_IDS))
    c.execute(
        f"UPDATE hypotheses SET status = 'rejected' "
        f"WHERE hypothesis_id IN ({ph}) AND status IN ('backtesting', 'draft')",
        REJECT_IDS,
    )
    print(f"Rejected: {c.rowcount} hypotheses")

    # 2. Reset to draft
    for hid in RESET_TO_DRAFT:
        c.execute(
            "UPDATE hypotheses SET status = 'draft' WHERE hypothesis_id = ?",
            (hid,),
        )
        print(f"Reset {hid} to draft: {c.rowcount}")

    # 3. Clean stale backtest events and stats
    all_ids = REJECT_IDS + RESET_TO_DRAFT
    ph2 = ",".join("?" * len(all_ids))
    c.execute(f"DELETE FROM backtest_events WHERE hypothesis_id IN ({ph2})", all_ids)
    print(f"Cleaned {c.rowcount} backtest events")
    c.execute(f"DELETE FROM hypothesis_stats WHERE hypothesis_id IN ({ph2})", all_ids)
    print(f"Cleaned {c.rowcount} stats rows")

    conn.commit()
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
