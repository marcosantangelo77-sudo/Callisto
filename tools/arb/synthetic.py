"""Cross-market synthetic arbs: team totals vs game total."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools.book_keys import canonicalize_book
from tools.math_utils import american_to_decimal

from tools.arb.models import (
    ArbLeg,
    ArbOpportunity,
    DEFAULT_BUDGET,
    DEFAULT_EPSILON,
    DEFAULT_STALE_SECONDS,
    MIN_PROFIT_PCT,
)
from tools.arb.prices import _age_seconds, _extract_line_ts


# ---------------------------------------------------------------------------
# Cross-market synthetic arbs.
# ---------------------------------------------------------------------------
def scan_cross_market_synthetic(
    game: dict,
    *,
    epsilon: float = DEFAULT_EPSILON,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
    budget: float = DEFAULT_BUDGET,
    now: Optional[datetime] = None,
    sport: str = "",
) -> list[ArbOpportunity]:
    """Team total (Over X.5) + opponent team total (Over Y.5) vs game total.

    If book A lets you buy team_total_over(home, X) + team_total_over(away, Y)
    cheaper than a direct game total Over(X+Y) at book B, there is a synthetic
    arb whose combined payoff dominates the direct market under the worst
    decomposition. Requires team totals to be present in the feed (not
    universally available — we silently return [] when absent).

    Tag = ``synthetic_arb``. Higher-risk than pure arb because the decomposition
    is correlation-sensitive; we only surface it when the price advantage is
    large enough to overcome any residual correlation premium.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Team-total markets in the odds-api feed are emitted as team_totals or
    # sport-specific variants. We probe for both forms.
    TEAM_TOTAL_KEYS = {"team_totals", "alternate_team_totals"}
    GAME_TOTAL_KEY = "totals"

    team_total_outcomes: dict[str, list[dict]] = defaultdict(list)
    game_total_outcomes: list[dict] = []

    for bm in game.get("bookmakers", []):
        bm_key = canonicalize_book(bm.get("key") or bm.get("title") or "")
        for mkt in bm.get("markets", []):
            mkey = mkt.get("key", "")
            if mkey in TEAM_TOTAL_KEYS:
                for o in mkt.get("outcomes", []):
                    name = (o.get("name") or "").lower()
                    if "over" not in name:
                        continue
                    desc = o.get("description") or o.get("team") or ""
                    if not desc:
                        continue
                    pt = o.get("point")
                    if pt is None:
                        continue
                    ts = _extract_line_ts(o, bm)
                    age = _age_seconds(ts, now)
                    if age is None or age > stale_seconds:
                        continue
                    try:
                        dec = american_to_decimal(int(o.get("price")))
                    except (TypeError, ValueError):
                        continue
                    team_total_outcomes[desc].append({
                        "bookmaker": bm.get("title", bm_key),
                        "bookmaker_canonical": bm_key,
                        "price": int(o["price"]),
                        "decimal": dec,
                        "point": pt,
                        "fetched_at": ts,
                        "age": age,
                    })
            elif mkey == GAME_TOTAL_KEY:
                for o in mkt.get("outcomes", []):
                    name = (o.get("name") or "").lower()
                    if "over" not in name:
                        continue
                    pt = o.get("point")
                    if pt is None:
                        continue
                    ts = _extract_line_ts(o, bm)
                    age = _age_seconds(ts, now)
                    if age is None or age > stale_seconds:
                        continue
                    try:
                        dec = american_to_decimal(int(o.get("price")))
                    except (TypeError, ValueError):
                        continue
                    game_total_outcomes.append({
                        "bookmaker": bm.get("title", bm_key),
                        "bookmaker_canonical": bm_key,
                        "price": int(o["price"]),
                        "decimal": dec,
                        "point": pt,
                        "fetched_at": ts,
                        "age": age,
                    })

    if len(team_total_outcomes) < 2 or not game_total_outcomes:
        return []

    home = game.get("home_team", "")
    away = game.get("away_team", "")
    arbs: list[ArbOpportunity] = []

    # Try every combination of (home_team_over, away_team_over) whose point
    # sum equals an available game total, and compare the pair against the
    # cheapest game total Over at that line from a DIFFERENT book.
    teams = list(team_total_outcomes.keys())
    if len(teams) != 2:
        return []  # only handle clean 2-team cases
    t1, t2 = teams

    for opt1 in team_total_outcomes[t1]:
        for opt2 in team_total_outcomes[t2]:
            combo_pt = opt1["point"] + opt2["point"]
            for gto in game_total_outcomes:
                if gto["point"] != combo_pt:
                    continue
                # Synthetic "cover" bet: bet BOTH team totals Over; if both
                # hit, you collect both tickets; if either misses, you need
                # the opponent Under equivalent, which we don't directly
                # have. Instead we compute the simpler edge: the "fair"
                # decimal on (game total Over X) is at least the decimal
                # of the less-informative team-total pair; if the individual
                # game total pays MORE than the product-style decomposition,
                # you'd buy the game total outright — no synthetic edge.
                #
                # We flag a synthetic arb when the GAME total Over pays so
                # much less (i.e. implied prob > combined team implied)
                # that backing team totals Over + matching side of game
                # total Under at another book beats breakeven.
                #
                # For clean reporting we only surface the magnitude and the
                # books involved; the executor module is responsible for
                # the correlation-aware sizing. This keeps the scanner from
                # making unverified correlation claims.
                combined_implied = (1.0 / opt1["decimal"]) + (1.0 / opt2["decimal"])
                game_implied = 1.0 / gto["decimal"]
                # Rough: if combined team-total implied < game-total implied
                # by more than epsilon, team totals are collectively cheaper.
                gap = game_implied - combined_implied
                if gap <= epsilon:
                    continue
                if opt1["bookmaker_canonical"] == gto["bookmaker_canonical"] \
                   and opt2["bookmaker_canonical"] == gto["bookmaker_canonical"]:
                    # Same book on all three legs — book already arbitrages
                    # itself; can't extract without across-book spread.
                    continue
                max_age = max(opt1["age"], opt2["age"], gto["age"])
                profit_pct = gap  # simplified
                if profit_pct < MIN_PROFIT_PCT:
                    continue
                effective_budget = budget  # no book-cap math in synthetic
                legs = [
                    ArbLeg(
                        bookmaker=opt1["bookmaker"],
                        bookmaker_canonical=opt1["bookmaker_canonical"],
                        outcome=f"{t1} Over {opt1['point']}",
                        american_odds=opt1["price"],
                        decimal_odds=opt1["decimal"],
                        implied_prob=1.0 / opt1["decimal"],
                        point=opt1["point"],
                        fetched_at=opt1["fetched_at"],
                        age_seconds=opt1["age"],
                    ),
                    ArbLeg(
                        bookmaker=opt2["bookmaker"],
                        bookmaker_canonical=opt2["bookmaker_canonical"],
                        outcome=f"{t2} Over {opt2['point']}",
                        american_odds=opt2["price"],
                        decimal_odds=opt2["decimal"],
                        implied_prob=1.0 / opt2["decimal"],
                        point=opt2["point"],
                        fetched_at=opt2["fetched_at"],
                        age_seconds=opt2["age"],
                    ),
                ]
                arbs.append(ArbOpportunity(
                    game_id=str(game.get("id", "")),
                    game=f"{away} @ {home}",
                    sport=sport or game.get("sport_key", ""),
                    market_type=f"synthetic:team_totals_vs_game_total_{combo_pt}",
                    thesis_tag="synthetic_arb",
                    total_implied=round(combined_implied, 6),
                    profit_pct=round(profit_pct, 6),
                    expected_profit=round(effective_budget * profit_pct, 2),
                    budget_requested=budget,
                    effective_budget=effective_budget,
                    legs=legs,
                    limited_by_book_caps=False,
                    max_leg_age_s=round(max_age, 1),
                    detected_at=now.isoformat(),
                    expires_at=(now + timedelta(seconds=60)).isoformat(),
                    notes=(
                        f"synthetic: team totals ({t1}+{t2}) over {combo_pt} "
                        f"cheaper than game total {gto['point']} at "
                        f"{gto['bookmaker']} by {gap:.4f} implied"
                    ),
                ))

    return arbs
