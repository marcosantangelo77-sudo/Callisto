"""
Historical SGP correlation calibration.

Walks ``backtest_events`` and ``paper_trades`` in the live DB, groups events
by ``(sport, event_id, game_date)``, then — for every pair of same-event legs
that correspond to canonical SGPLeg archetypes — computes an empirical
Pearson correlation between the leg outcomes.

Each "leg outcome" is a Bernoulli (``actual_result`` ∈ {win, loss, push}). For
a 0/1 variable, Pearson correlation reduces to the phi coefficient, but we
keep the Pearson formulation so we can generalize later to stats-level
correlations (e.g. measured pass yards instead of win/loss).

Writes: ``config/sgp_correlations_empirical.yaml``. The scanner's
``sgp_correlations.load()`` picks this up automatically on next start.

Usage
-----

    # Calibrate against the live DB (read-only — we only SELECT)
    python scripts/calibrate_sgp_correlations.py

    # Against a specific path (safer for one-off explorations)
    python scripts/calibrate_sgp_correlations.py --db path/to/callisto.db

    # Drop the min-sample floor for a first-pass survey
    python scripts/calibrate_sgp_correlations.py --min-samples 5

Output also goes to stdout as a human-readable top-correlations table.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

# Make sibling-package imports work when run as a script
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("calibrate_sgp_correlations")


# ---------------------------------------------------------------------------
# Leg archetype mapping
# ---------------------------------------------------------------------------
# Map raw (market, side) tuples from backtest_events / paper_trades into the
# canonical SGPLeg archetypes that the scanner reasons about. Unmapped rows
# are skipped silently.

def _canonical_leg(market: str, side: str, player: Optional[str]) -> Optional[str]:
    mk = (market or "").lower().strip()
    sd_raw = (side or "").strip()
    sd = sd_raw.lower()
    # backtest_events stores h2h/spreads sides as the team name (e.g. "San
    # Francisco Giants"). Anything that isn't over/under/push/empty is treated
    # as a team-picking leg -> "win".
    if sd and sd not in ("over", "under", "push", ""):
        sd = "win"
    if mk in ("h2h", "moneyline", "ml"):
        return "team_ml_win"
    if mk in ("spreads", "spread", "team_spread"):
        return "team_spread_cover"
    if mk in ("totals", "total"):
        return f"game_total_{sd}" if sd in ("over", "under") else None
    if mk in ("team_totals", "team_total"):
        return f"team_total_{sd}" if sd in ("over", "under") else None
    prop_map = {
        "player_pass_yds": f"qb_pass_yds_{sd}",
        "player_pass_tds": f"qb_pass_tds_{sd}",
        "player_rush_yds": f"rb_rush_yds_{sd}",
        "player_receiving_yds": f"wr_rec_yds_{sd}",
        "player_receptions": f"wr_rec_{sd}",
        "player_points": f"player_pts_{sd}",
        "player_assists": f"player_ast_{sd}",
        "player_rebounds": f"player_reb_{sd}",
        "player_threes": f"player_threes_{sd}",
        "batter_hits": f"batter_hits_{sd}",
        "batter_total_bases": f"batter_tb_{sd}",
        "batter_rbi": f"batter_rbi_{sd}",
        "batter_home_runs": f"batter_hr_{sd}",
        "pitcher_strikeouts": f"pitcher_ks_{sd}",
    }
    return prop_map.get(mk) if sd in ("over", "under") else None


def _result_bernoulli(actual: Optional[str]) -> Optional[int]:
    """Encode ``actual_result`` into 0/1. Pushes are dropped entirely."""
    if actual is None:
        return None
    a = str(actual).lower().strip()
    if a in ("win", "won", "hit", "1", "true"):
        return 1
    if a in ("loss", "lost", "miss", "0", "false", "lose"):
        return 0
    return None


def _are_complementary(a: str, b: str) -> bool:
    """Two legs are logical complements if they differ only by over<->under
    (or win<->lose). Empirical correlation of complements is trivially -1 and
    carries no SGP-pricing information."""
    for pair in (("_over", "_under"),):
        if a.endswith(pair[0]) and b.endswith(pair[1]) and a[: -len(pair[0])] == b[: -len(pair[1])]:
            return True
        if b.endswith(pair[0]) and a.endswith(pair[1]) and b[: -len(pair[0])] == a[: -len(pair[1])]:
            return True
    return False


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 4:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _connect(db_path: str, *, read_only: bool = True) -> sqlite3.Connection:
    """Open the DB read-only via URI by default. This keeps a live Callisto
    safe (no accidental writes, no WAL checkpoint races) and works even when
    the file is attached to a live process."""
    if read_only:
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _fetch_rows(db: sqlite3.Connection) -> list[dict]:
    """Union rows from backtest_events and paper_trades with the columns we
    need for correlation computation. Missing columns from either table are
    tolerated via COALESCE/NULLs."""
    rows: list[dict] = []
    for table in ("backtest_events", "paper_trades"):
        # Probe table existence
        cur = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if not cur.fetchone():
            continue
        # Introspect columns so we tolerate pre-local_game_date schemas
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        required = {"sport", "event_id", "market", "side", "actual_result"}
        if not required.issubset(cols):
            logger.warning(
                "Skipping %s: missing required columns %s",
                table, required - cols,
            )
            continue
        date_expr = (
            "COALESCE(local_game_date, game_date)" if "local_game_date" in cols
            else "game_date"
        )
        player_col = "player" if "player" in cols else "NULL"
        q = f"""
            SELECT
                sport,
                event_id,
                {date_expr} AS game_date,
                market,
                side,
                {player_col} AS player,
                actual_result
            FROM {table}
            WHERE actual_result IS NOT NULL
              AND actual_result <> 'push'
              AND actual_result <> 'pending'
        """
        try:
            for r in db.execute(q):
                rows.append(dict(r))
        except sqlite3.OperationalError as e:
            logger.warning("Could not read %s: %s", table, e)
    return rows


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def calibrate(rows: list[dict], min_samples: int = 30) -> dict:
    """Return a nested dict ``{sport: {"leg_a|leg_b": rho}}`` for pairs with
    at least ``min_samples`` joint observations."""
    # Bucket by (sport, event_id)
    by_event: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    skipped_unmapped = 0
    skipped_bad_result = 0
    total_legs = 0
    for r in rows:
        leg = _canonical_leg(r.get("market"), r.get("side"), r.get("player"))
        if leg is None:
            skipped_unmapped += 1
            continue
        val = _result_bernoulli(r.get("actual_result"))
        if val is None:
            skipped_bad_result += 1
            continue
        sport = (r.get("sport") or "").lower()
        # Normalize odds-api sport keys
        for prefix in (
            "americanfootball_",
            "basketball_",
            "baseball_",
            "icehockey_",
        ):
            if sport.startswith(prefix):
                sport = sport[len(prefix):]
                break
        evt = str(r.get("event_id") or "")
        if not evt:
            continue
        by_event[(sport, evt)].append((leg, val))
        total_legs += 1

    logger.info(
        "Loaded %d legs across %d events (skipped %d unmapped, %d bad result)",
        total_legs, len(by_event), skipped_unmapped, skipped_bad_result,
    )

    # For each event, enumerate all leg pairs; accumulate paired observations
    # keyed by (sport, leg_a, leg_b) with leg_a < leg_b for determinism.
    pair_obs: dict[tuple[str, str, str], list[tuple[int, int]]] = defaultdict(list)
    for (sport, _evt), legs in by_event.items():
        if len(legs) < 2:
            continue
        # Deduplicate within one event on (leg_type) — we want one observation
        # per (event, leg_type). If the same leg appears twice at different
        # books, pick the majority result (ties -> first).
        by_leg: dict[str, list[int]] = defaultdict(list)
        for leg, val in legs:
            by_leg[leg].append(val)
        collapsed: dict[str, int] = {}
        for leg, vals in by_leg.items():
            collapsed[leg] = 1 if sum(vals) * 2 >= len(vals) else 0
        leg_names = sorted(collapsed.keys())
        for i, a in enumerate(leg_names):
            for b in leg_names[i + 1:]:
                # Skip trivial complementary pairs (over+under of same thing)
                # which always produce rho ≈ -1 and aren't useful SGP info.
                if _are_complementary(a, b):
                    continue
                pair_obs[(sport, a, b)].append((collapsed[a], collapsed[b]))

    # Compute Pearson per pair
    out: dict[str, dict[str, float]] = defaultdict(dict)
    meta_rows: list[tuple[str, str, str, int, float]] = []
    for (sport, a, b), obs in pair_obs.items():
        if len(obs) < min_samples:
            continue
        xs = [o[0] for o in obs]
        ys = [o[1] for o in obs]
        rho = _pearson(xs, ys)
        if rho is None:
            continue
        rho = max(-1.0, min(1.0, round(rho, 4)))
        out[sport][f"{a}|{b}"] = rho
        meta_rows.append((sport, a, b, len(obs), rho))

    # Top-N for stdout
    meta_rows.sort(key=lambda r: abs(r[4]), reverse=True)
    logger.info("Calibrated %d leg pairs with n>=%d", len(meta_rows), min_samples)
    for sport, a, b, n, rho in meta_rows[:15]:
        logger.info("  %-10s %-30s + %-30s  n=%-4d  rho=%+0.3f", sport, a, b, n, rho)

    return dict(out)


def write_yaml(out: dict, path: Path) -> None:
    """Write the calibrated correlations in our flat-YAML format."""
    lines: list[str] = ["# Auto-generated by scripts/calibrate_sgp_correlations.py"]
    lines.append("# Do not edit by hand. Re-run the calibration script instead.")
    lines.append("# Format: sport -> \"leg_a|leg_b\": rho")
    lines.append("")
    for sport in sorted(out.keys()):
        lines.append(f"{sport}:")
        for key in sorted(out[sport].keys()):
            rho = out[sport][key]
            lines.append(f"  {key}: {rho:+0.4f}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote %s", path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--db",
        default=os.getenv("CALLISTO_DB_PATH", "memory/callisto.db"),
        help="Path to Callisto DB (default: memory/callisto.db)",
    )
    ap.add_argument(
        "--out",
        default="config/sgp_correlations_empirical.yaml",
        help="Output YAML path",
    )
    ap.add_argument(
        "--min-samples",
        type=int,
        default=30,
        help="Minimum joint observations to emit a correlation (default: 30)",
    )
    args = ap.parse_args()

    db_path = args.db
    if not Path(db_path).is_file():
        logger.error("DB not found: %s", db_path)
        return 2

    conn = _connect(db_path)
    try:
        rows = _fetch_rows(conn)
    finally:
        conn.close()

    if not rows:
        logger.warning("No resolved events found — empirical YAML will be empty.")

    out = calibrate(rows, min_samples=args.min_samples)
    write_yaml(out, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
