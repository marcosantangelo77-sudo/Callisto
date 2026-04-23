"""Retro-backtest proxy for mlb_quiet_innings thesis.

We don't have inning-by-inning historical data yet (that's what
live_game_states will collect going forward). So this is a PROXY: we
ask whether games whose FINAL total exceeded the pre-game line by 1+
runs are common enough that an in-game "over-reaction" detector would
have positive expectation. This is a ceiling check, not a validation.

Uses game_results joined to odds_snapshots (pre-game totals) to compute
the distribution of (final_total - pregame_total) for MLB 2026. The
thesis says: after a quiet start, live total drops. If we OVER-bet at
the live (dropped) total, we need the FINAL total to recover past the
LIVE total. We can't see the live total; we approximate "live total"
as pregame - 1.5 (typical drop after 3-4 scoreless innings based on
book behavior — confirmable once we have live_game_states history).
"""

from __future__ import annotations

import sqlite3
import statistics

DB = "C:/Users/marco/OneDrive/Desktop/Callisto/memory/callisto.db"


def main() -> None:
    db = sqlite3.connect(DB)
    # Pre-game totals: use backtest_events total_line for totals market.
    rows = db.execute(
        """
        SELECT be.event_id, be.game_date, be.line AS total_line,
               gr.total_score
        FROM backtest_events be
        JOIN game_results gr
          ON gr.sport='baseball_mlb'
          AND gr.game_date = be.game_date
          AND (gr.home_team = be.event_id OR gr.away_team = be.event_id
               OR be.event_id LIKE '%'||gr.home_team||'%'
               OR be.event_id LIKE '%'||gr.away_team||'%')
        WHERE be.sport='baseball_mlb'
          AND be.market = 'totals'
          AND be.line IS NOT NULL
          AND gr.total_score IS NOT NULL
        LIMIT 5000
        """
    ).fetchall()
    print(f"joined sample: {len(rows)} MLB games with pregame total + final")
    if not rows:
        # Fall back: distribution of MLB finals alone.
        gr = db.execute(
            "SELECT total_score FROM game_results "
            "WHERE sport='baseball_mlb' AND total_score IS NOT NULL"
        ).fetchall()
        totals = [r[0] for r in gr if r[0] is not None]
        if totals:
            print(f"fallback — {len(totals)} MLB finals: "
                  f"mean={statistics.mean(totals):.2f} "
                  f"median={statistics.median(totals)} "
                  f"pct_over_8={sum(1 for t in totals if t>8)/len(totals):.1%} "
                  f"pct_over_9={sum(1 for t in totals if t>9)/len(totals):.1%}")
            # Rough proxy: if live total after quiet 4 innings ≈ 6.5-7.0
            # and typical pregame was ≈8.5, then OVER cashes when
            # final > 6.5. Fraction of finals > 6.5 is our ceiling rate.
            for cut in (6.0, 6.5, 7.0, 7.5):
                r = sum(1 for t in totals if t > cut) / len(totals)
                print(f"pct final > {cut}: {r:.1%}")
            print("break-even at -110 juice: 52.4%")
        return

    diffs = [r[-1] - r[2] for r in rows]
    mean = statistics.mean(diffs)
    pct_beat = sum(1 for d in diffs if d > 0) / len(diffs)
    pct_hit_proxy = sum(1 for d in diffs if d > -1.5) / len(diffs)
    print(f"final - pregame: mean={mean:+.2f}")
    print(f"pct final > pregame: {pct_beat:.1%}")
    print(f"pct final > (pregame - 1.5) [proxy]: {pct_hit_proxy:.1%}")
    print(f"break-even at -110 juice: 52.4%")
    print("verdict: " + (
        "signal plausible — proxy clears juice"
        if pct_hit_proxy > 0.524 else
        "signal unclear in proxy — needs live_game_states to validate"
    ))


if __name__ == "__main__":
    main()
