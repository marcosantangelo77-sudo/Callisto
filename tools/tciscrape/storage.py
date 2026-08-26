"""SQLite persistence for TCI scores and matchup queries."""

import json
import logging

import aiosqlite

from tools.tciscrape.constants import DB_PATH

logger = logging.getLogger("callisto.tci")


async def _store_tci_results(results: list[dict], season: int, db_path: str = DB_PATH) -> None:
    """Store TCI results in the database."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tci_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL,
                team_id TEXT,
                sport TEXT NOT NULL DEFAULT 'basketball_ncaaw',
                season INTEGER NOT NULL,
                tci_score REAL,
                task_cohesion REAL,
                social_cohesion REAL,
                stability_score REAL,
                geographic_concentration REAL,
                top_region TEXT,
                state_concentration REAL,
                experience_ratio REAL,
                class_balance REAL,
                continuity_proxy REAL,
                coaching_tenure_years INTEGER,
                coaching_stability REAL,
                religious_affiliation TEXT,
                institutional_factor REAL,
                international_players INTEGER,
                domestic_players INTEGER,
                roster_size INTEGER,
                full_data TEXT,
                computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_name, sport, season)
            )
        """)

        for r in results:
            await db.execute(
                """INSERT OR REPLACE INTO tci_scores
                (team_name, team_id, sport, season, tci_score,
                 task_cohesion, social_cohesion, stability_score,
                 geographic_concentration, top_region, state_concentration,
                 experience_ratio, class_balance, continuity_proxy,
                 coaching_tenure_years, coaching_stability, religious_affiliation,
                 institutional_factor, international_players, domestic_players,
                 roster_size, full_data)
                VALUES (?, ?, 'basketball_ncaaw', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["team_name"], r.get("team_id"), season,
                    r["tci_score"], r.get("task_cohesion", 0),
                    r.get("social_cohesion", 0), r.get("stability_score", 0),
                    r["geographic_concentration"], r["top_region"],
                    r.get("state_concentration", 0), r["experience_ratio"],
                    r.get("class_balance", 0), r.get("continuity_proxy", 0),
                    r["coaching_tenure_years"], r["coaching_stability"],
                    r["religious_affiliation"], r.get("institutional_factor", 0),
                    r["international_players"], r.get("domestic_players", 0),
                    r["roster_size"], json.dumps(r),
                ),
            )

        await db.commit()
        logger.info(f"TCI: Stored {len(results)} team cohesion profiles")


async def get_tci_matchup(
    home_team: str, away_team: str, season: int = 2026, db_path: str = DB_PATH
) -> dict:
    """
    Get TCI comparison for a matchup.

    Returns both teams' TCI scores, the differential, AND decomposed
    sub-signals (experience ratio, stability score) with threshold filtering.

    Backtest evidence (NCAAW 2026, n=52):
      - Composite TCI: 51.9% (flat, no signal)
      - Experience Ratio: 59.6% win rate, +13.8% ROI, p=0.17 -- STRONGEST
      - Stability Score: 57.7% win rate, +10.1% ROI, p=0.27 -- SECOND
      - Social cohesion, task cohesion, coaching tenure alone: no signal
      - Only predictive when |differential| >= 10 (57.1%), very strong >= 15 (66.7%)
    """
    # Imported here to avoid a circular import (pipeline -> storage is fine;
    # signals are pure so this stays lightweight).
    from tools.tciscrape.signals import (
        get_experience_signal,
        get_stability_signal,
    )

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        results = {}
        for team in [home_team, away_team]:
            cursor = await db.execute(
                "SELECT full_data FROM tci_scores WHERE team_name LIKE ? AND season = ?",
                (f"%{team}%", season),
            )
            row = await cursor.fetchone()
            if row:
                results[team] = json.loads(row[0])
            else:
                results[team] = {"tci_score": 0, "error": "not found"}

    home_data = results.get(home_team, {})
    away_data = results.get(away_team, {})

    home_tci = home_data.get("tci_score", 0)
    away_tci = away_data.get("tci_score", 0)

    # --- Decomposed sub-signals (backtest-proven) ---
    home_exp = home_data.get("experience_ratio", 0)
    away_exp = away_data.get("experience_ratio", 0)
    home_stab = home_data.get("stability_score", 0)
    away_stab = away_data.get("stability_score", 0)

    # Experience ratio: scale to 0-100 for differential comparison
    # Raw experience_ratio is 0.0-1.0, multiply by 100 for parity with other scores
    exp_diff = round((home_exp - away_exp) * 100, 1)
    stab_diff = round(home_stab - away_stab, 1)

    # Decomposed signals with threshold filtering
    exp_signal = get_experience_signal(home_data, away_data)
    stab_signal = get_stability_signal(home_data, away_data)

    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_tci": home_data,
        "away_tci": away_data,
        # Composite (kept for reference, NOT used as betting signal)
        "tci_differential": round(home_tci - away_tci, 1),
        "cohesion_edge": "home" if home_tci > away_tci else "away",
        # Decomposed sub-signals (USE THESE for betting)
        "experience_ratio_differential": exp_diff,
        "stability_score_differential": stab_diff,
        "experience_signal": exp_signal,
        "stability_signal": stab_signal,
    }
