"""Schedule-context computation (DB-backed).

Extracted verbatim from tools/backtest_io.py.
"""

from datetime import timedelta

logger = __import__("logging").getLogger("callisto.backtest")


async def build_schedule_context(
    db,
    sport: str, start_date: str, end_date: str,
    live_games: list[tuple[str, str, str]] | None = None,
) -> dict:
    """Pre-compute schedule context for all games in a date range.

    Args:
        live_games: Optional list of (game_date, home_team, away_team) for
            upcoming games not yet in game_results (e.g. today's live odds).
            Context will be computed for these using historical team data.

    Returns dict keyed by (game_date, home_team, away_team) with context:
        home_days_rest / away_days_rest: int
        home_b2b / away_b2b: bool — team played yesterday
        home_road_streak / away_road_streak: int — consecutive away games
        home_games_in_4 / away_games_in_4: int — schedule density
        home_prev_margin / away_prev_margin: float
        is_revenge: bool — teams played recently
        home_sandwich / away_sandwich: bool — game squeezed between two others
        home_win_pct / away_win_pct: float — season record approximation
    """
    from datetime import datetime as dt

    buffer_start = dt.strptime(start_date, "%Y-%m-%d") - timedelta(days=30)
    buffer_start_str = buffer_start.strftime("%Y-%m-%d")

    rows = await db.execute_fetchall(
        """SELECT game_date, home_team, away_team, home_score, away_score,
                  total_score, spread_result, winner
           FROM game_results
           WHERE sport = ? AND game_date >= ? AND game_date <= ?
           ORDER BY game_date""",
        (sport, buffer_start_str, end_date),
    )

    if not rows:
        return {}

    # Build per-team game lists
    team_games: dict[str, list] = {}
    for r in rows:
        gd, home, away, hs, as_, ts, sr, winner = r
        hs = hs or 0
        as_ = as_ or 0
        home_margin = hs - as_
        team_games.setdefault(home, []).append(
            (gd, away, True, home_margin, winner)
        )
        team_games.setdefault(away, []).append(
            (gd, home, False, -home_margin, winner)
        )

    for t in team_games:
        team_games[t].sort(key=lambda x: x[0])

    context = {}
    for r in rows:
        gd, home, away = r[0], r[1], r[2]
        if gd < start_date:
            continue

        ctx: dict = {}
        for team, prefix in [(home, "home"), (away, "away")]:
            tg = team_games.get(team, [])
            opp = away if prefix == "home" else home
            is_home_side = prefix == "home"
            idx = None
            for i, g in enumerate(tg):
                if g[0] == gd and g[2] == is_home_side and g[1] == opp:
                    idx = i
                    break
            if idx is None:
                ctx[f"{prefix}_days_rest"] = 99
                ctx[f"{prefix}_b2b"] = False
                ctx[f"{prefix}_road_streak"] = 0
                ctx[f"{prefix}_games_in_4"] = 1
                ctx[f"{prefix}_prev_margin"] = 0.0
                continue

            # Days rest
            if idx > 0:
                prev_date = tg[idx - 1][0]
                d1 = dt.strptime(gd, "%Y-%m-%d")
                d0 = dt.strptime(prev_date, "%Y-%m-%d")
                days_rest = (d1 - d0).days
                prev_margin = tg[idx - 1][3]
            else:
                days_rest = 99
                prev_margin = 0.0

            ctx[f"{prefix}_days_rest"] = days_rest
            ctx[f"{prefix}_b2b"] = (days_rest == 1)
            ctx[f"{prefix}_prev_margin"] = prev_margin

            # Road streak
            road_streak = 0
            if not is_home_side:
                for j in range(idx, -1, -1):
                    if not tg[j][2]:
                        road_streak += 1
                    else:
                        break
            else:
                for j in range(idx - 1, -1, -1):
                    if not tg[j][2]:
                        road_streak += 1
                    else:
                        break
            ctx[f"{prefix}_road_streak"] = road_streak

            # Games in last 4 days (schedule density)
            game_dt = dt.strptime(gd, "%Y-%m-%d")
            four_days_ago = (game_dt - timedelta(days=4)).strftime("%Y-%m-%d")
            games_in_4 = sum(1 for g in tg if four_days_ago < g[0] <= gd)
            ctx[f"{prefix}_games_in_4"] = games_in_4

        # Revenge game: teams played in last 30 days
        home_games = team_games.get(home, [])
        ctx["is_revenge"] = any(
            g[1] == away and g[0] < gd and g[0] >= buffer_start_str
            for g in home_games
        )

        # Sandwich game: game within 2 days before AND within 2 days after
        for team, prefix in [(home, "home"), (away, "away")]:
            tg = team_games.get(team, [])
            game_dt = dt.strptime(gd, "%Y-%m-%d")
            has_prev_close = any(
                0 < (game_dt - dt.strptime(g[0], "%Y-%m-%d")).days <= 2
                for g in tg if g[0] < gd
            )
            has_next_close = any(
                0 < (dt.strptime(g[0], "%Y-%m-%d") - game_dt).days <= 2
                for g in tg if g[0] > gd
            )
            ctx[f"{prefix}_sandwich"] = has_prev_close and has_next_close

        # Team records for playoff standing approximation
        for team, prefix in [(home, "home"), (away, "away")]:
            tg = team_games.get(team, [])
            wins = sum(1 for g in tg if g[0] < gd and g[4] == team)
            losses = sum(1 for g in tg if g[0] < gd and g[4] and g[4] != team)
            ctx[f"{prefix}_wins"] = wins
            ctx[f"{prefix}_losses"] = losses
            total = wins + losses
            ctx[f"{prefix}_win_pct"] = wins / total if total > 0 else 0.5

        context[(gd, home, away)] = ctx

    # ── Augment with live/upcoming games not yet in game_results ──
    # Paper trading needs context for today's games, which haven't been
    # played yet and so aren't in game_results.  Compute their schedule
    # factors from the same team_games history.
    if live_games:
        added = 0
        for lg_date, lg_home, lg_away in live_games:
            key = (lg_date, lg_home, lg_away)
            if key in context:
                continue  # already computed from game_results
            ctx = {}
            for team, prefix, is_home_side in [
                (lg_home, "home", True),
                (lg_away, "away", False),
            ]:
                tg = team_games.get(team, [])
                # Find most recent game before lg_date
                prev = [g for g in tg if g[0] < lg_date]
                if prev:
                    last = prev[-1]
                    d1 = dt.strptime(lg_date, "%Y-%m-%d")
                    d0 = dt.strptime(last[0], "%Y-%m-%d")
                    days_rest = (d1 - d0).days
                    prev_margin = last[3]
                else:
                    days_rest = 99
                    prev_margin = 0.0
                ctx[f"{prefix}_days_rest"] = days_rest
                ctx[f"{prefix}_b2b"] = (days_rest == 1)
                ctx[f"{prefix}_prev_margin"] = prev_margin

                # Road streak
                road_streak = 0
                for g in reversed(prev):
                    if not g[2]:  # away game
                        road_streak += 1
                    else:
                        break
                ctx[f"{prefix}_road_streak"] = road_streak

                # Games in last 4 days
                game_dt_live = dt.strptime(lg_date, "%Y-%m-%d")
                four_days_ago = (game_dt_live - timedelta(days=4)).strftime("%Y-%m-%d")
                games_in_4 = sum(1 for g in tg if four_days_ago < g[0] <= lg_date)
                ctx[f"{prefix}_games_in_4"] = max(games_in_4, 1)

                # Win pct from all prior games
                wins = sum(1 for g in tg if g[0] < lg_date and g[4] == team)
                losses = sum(1 for g in tg if g[0] < lg_date and g[4] and g[4] != team)
                ctx[f"{prefix}_wins"] = wins
                ctx[f"{prefix}_losses"] = losses
                total = wins + losses
                ctx[f"{prefix}_win_pct"] = wins / total if total > 0 else 0.5

            # Revenge game
            home_prev = [g for g in team_games.get(lg_home, []) if g[0] < lg_date]
            ctx["is_revenge"] = any(
                g[1] == lg_away and g[0] >= buffer_start_str for g in home_prev
            )

            # Sandwich game
            for team, prefix in [(lg_home, "home"), (lg_away, "away")]:
                tg = team_games.get(team, [])
                game_dt_live = dt.strptime(lg_date, "%Y-%m-%d")
                has_prev_close = any(
                    0 < (game_dt_live - dt.strptime(g[0], "%Y-%m-%d")).days <= 2
                    for g in tg if g[0] < lg_date
                )
                has_next_close = any(
                    0 < (dt.strptime(g[0], "%Y-%m-%d") - game_dt_live).days <= 2
                    for g in tg if g[0] > lg_date
                )
                ctx[f"{prefix}_sandwich"] = has_prev_close and has_next_close

            context[key] = ctx
            added += 1
        if added:
            logger.info(
                f"Schedule context: augmented with {added} live games "
                f"(total now {len(context)})"
            )

    logger.info(
        f"Schedule context: computed for {len(context)} games "
        f"({sport}, {start_date} to {end_date})"
    )
    return context

# Factors that ARE now filterable via schedule context.
# When adding a new derivable factor: implement it in _build_schedule_context,
# add matching logic in _game_matches_context_filter, and list it here.
