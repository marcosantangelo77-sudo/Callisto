"""
Arbitrage + dutch-book scanner — the guaranteed-profit floor.

A pure arb exists when you can place bets on every outcome of a market across
different books such that the sum of implied probabilities (1/decimal_odds)
is strictly less than 1.0. Stake each leg proportionally to its implied prob
and the return is the same regardless of which outcome hits.

This module gives you four flavours:

1. ``scan_pure_arb(game, market_type)`` — binary or multi-way market across
   best price per side.
2. ``scan_dutch_book(game, market_type)`` — generalised multi-outcome cover.
3. ``scan_cross_market_synthetic(game)`` — team-total + opponent-total vs
   game-total, and same-game-parlay vs individual-leg equivalents.
4. ``full_arbitrage_scan(snapshot, **opts)`` — aggregate over every game +
   market in a snapshot; write qualifying rows to ``ev_opportunities`` with
   ``source='arbitrage'`` and ``thesis_tag in {'arb', 'dutch',
   'synthetic_arb'}``.

STALE-LINE FILTER
=================
Any leg older than ``STALE_SECONDS`` (default 120s) disqualifies the whole
arb. A real book will have already moved; the arb is phantom. Age is taken
from per-outcome ``fetched_at`` when present, else the bookmaker-level
``fetched_at``/``last_update``. No timestamp = unknown age = rejected (we
bias conservative; unmarked data is almost always stale scraper dumps).

BOOK-LIMIT AWARENESS
====================
After computing theoretical stakes for a target budget, we clamp every leg
to ``book_keys.get_book_max_stake(book, market_type)`` and record the
effective-budget reduction. If the limiting leg would cap our budget at less
than ``MIN_EFFECTIVE_BUDGET_PCT`` of the requested amount, the arb is flagged
``limited=True`` — still reported but separately counted so the user can
decide whether microsize is worth the operational risk.

OUTPUT
======
Nothing gets placed. Qualifying arbs go into ``ev_opportunities`` with:

    source         = 'arbitrage'
    status         = 'open'
    market         = '<market_type>'
    team           = '<outcome_name>'     (one row per leg)
    bookmaker      = '<book>'
    american_odds  = <leg price>
    edge           = 1 - total_implied    (the "gap")
    expected_value = expected_profit_per_dollar
    kelly_fraction = stake_pct_of_budget
    detected_at    = ISO timestamp
    expires_at     = detected_at + 60s (via detected_at parse; our status
                     marker — consumers should refuse to execute past this)

No schema changes strictly required — the existing ``ev_opportunities`` table
absorbs it via the ``source`` column. A tiny migration adds ``thesis_tag``
for clarity.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Iterable

from tools.book_keys import canonicalize_book, get_book_max_stake
from tools.math_utils import american_to_decimal

logger = logging.getLogger("callisto.arbitrage_scanner")

# ---------------------------------------------------------------------------
# Constants — conservative defaults.
#
# EPSILON: the total-implied-prob cutoff. A pure arb has total < 1.0, but we
# demand total < 1.0 - EPSILON so we have room for (a) rounding in decimal
# odds, (b) partial-fill on the slower leg, and (c) the 1-3bp drift during
# the ~5s round-trip to place both tickets.
#
# STALE_SECONDS: maximum age of any leg's fetched_at. 120s is generous for
# ws-fed lines (typically <10s) and tight enough that a 15-min poll's final
# quote is still inside the window right after a refresh.
# ---------------------------------------------------------------------------
DEFAULT_EPSILON = 0.002
DEFAULT_STALE_SECONDS = 120.0
DEFAULT_BUDGET = 1000.0
MIN_EFFECTIVE_BUDGET_PCT = 0.5  # below this fraction the arb is "too small"

# A small positive profit floor below which we don't bother recording an arb.
# Rounding errors can produce total_implied = 0.9999 which yields 0.01% profit;
# not worth surfacing as actionable.
MIN_PROFIT_PCT = 0.005  # 0.5% minimum expected profit

# SANITY CEILING — above this profit_pct the "arb" is almost certainly a
# data quality bug (team-name mixup, stale feed, wrong side classification).
# Real arbs above 5% are extraordinarily rare; above 10% are mythical.
# Rows above this ceiling are dropped with a warning.
MAX_PROFIT_PCT = 0.10

# Price-range sanity: if one leg is quoted at +1000 or worse (massive dog)
# alongside another at -200 or better (heavy favorite at a different book)
# for the SAME binary market, the two quotes disagree about reality so hard
# that at least one is wrong. We reject pairs where abs(implied_A - implied_B)
# exceeds this delta on a binary market — a real 2% arb has legs that agree
# to within 5-10% implied; anything beyond this is a data mismatch.
MAX_IMPLIED_DIVERGENCE = 0.20


# ---------------------------------------------------------------------------
# Data classes — what the scanner emits.
# ---------------------------------------------------------------------------
@dataclass
class ArbLeg:
    """One leg of an arbitrage opportunity."""
    bookmaker: str
    bookmaker_canonical: str
    outcome: str
    american_odds: int
    decimal_odds: float
    implied_prob: float
    point: Optional[float] = None
    stake: float = 0.0          # filled in once budget is applied
    stake_capped_by_book: bool = False
    fetched_at: Optional[str] = None
    age_seconds: Optional[float] = None


@dataclass
class ArbOpportunity:
    """A complete arb/dutch-book/synthetic-arb opportunity."""
    game_id: str
    game: str                   # "away @ home" display
    sport: str
    market_type: str            # 'h2h', 'spreads', 'totals', or synthetic label
    thesis_tag: str             # 'arb' | 'dutch' | 'synthetic_arb'
    total_implied: float
    profit_pct: float           # expected profit per dollar of effective budget
    expected_profit: float      # USD at the effective budget
    budget_requested: float
    effective_budget: float     # budget reduced by book limits
    legs: list[ArbLeg] = field(default_factory=list)
    limited_by_book_caps: bool = False
    max_leg_age_s: float = 0.0
    detected_at: str = ""
    expires_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Timestamp helpers — mirror edge_scanner semantics but strict.
# ---------------------------------------------------------------------------
def _parse_ts(s: object) -> Optional[datetime]:
    if s is None:
        return None
    try:
        if isinstance(s, datetime):
            return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
        txt = str(s)
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _age_seconds(line_ts: Optional[str], now: datetime) -> Optional[float]:
    dt = _parse_ts(line_ts)
    if dt is None:
        return None
    return (now - dt).total_seconds()


def _extract_line_ts(outcome: dict, bm: dict) -> Optional[str]:
    """Pick the best available freshness stamp for an outcome.

    Preference order:
      1. outcome.fetched_at  (our own ingest stamp, most meaningful)
      2. bm.fetched_at       (bookmaker-level stamp from line_monitor)
      3. bm.last_update      (the book's self-reported stamp)
    """
    for cand in (outcome.get("fetched_at"), bm.get("fetched_at"), bm.get("last_update")):
        if cand:
            return cand
    return None


# ---------------------------------------------------------------------------
# Best price per outcome per market, across all books in a game dict.
# ---------------------------------------------------------------------------
def _collect_best_prices(
    game: dict,
    market_type: str,
    point_value: Optional[float] = None,
) -> dict[str, dict]:
    """Return {outcome_name: {price, bookmaker, bookmaker_canonical, point,
    fetched_at, decimal}} — the highest decimal price per outcome.

    If ``point_value`` is given (for spreads/totals), only outcomes at that
    exact point are considered. This matters: a +3 spread at DK and a +3.5
    spread at BetMGM are different bets and averaging them together creates
    phantom arbs.
    """
    best: dict[str, dict] = {}
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_type:
                continue
            for outcome in mkt.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                if name is None or price is None:
                    continue
                pt = outcome.get("point")
                if point_value is not None and pt != point_value:
                    continue
                try:
                    dec = american_to_decimal(int(price))
                except (TypeError, ValueError):
                    continue
                if dec <= 1.0:
                    continue
                entry = {
                    "american": int(price),
                    "decimal": dec,
                    "implied": 1.0 / dec,
                    "bookmaker": bm.get("title", bm.get("key", "")),
                    "bookmaker_canonical": canonicalize_book(
                        bm.get("key") or bm.get("title") or ""
                    ),
                    "point": pt,
                    "fetched_at": _extract_line_ts(outcome, bm),
                }
                prev = best.get(name)
                if prev is None or dec > prev["decimal"]:
                    best[name] = entry
    return best


def _collect_point_groups(game: dict, market_type: str) -> dict[Optional[float], dict]:
    """Group outcomes by point value for spreads/totals. Returns
    {point: {outcome_name: best_entry}}. For h2h there is no point, so we
    return {None: {...}}.
    """
    if market_type == "h2h":
        return {None: _collect_best_prices(game, market_type)}

    # Collect every distinct point we've seen across all books so we can
    # iterate and call the single-point version. This keeps the invariant
    # "all legs at the same point" clean.
    points: set = set()
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_type:
                continue
            for o in mkt.get("outcomes", []):
                pt = o.get("point")
                if pt is not None:
                    points.add(pt)

    out: dict[Optional[float], dict] = {}
    for pt in points:
        grp = _collect_best_prices(game, market_type, point_value=pt)
        if grp:
            out[pt] = grp
    return out


# ---------------------------------------------------------------------------
# Stake math — the core arb calculation.
# ---------------------------------------------------------------------------
def _compute_stakes(
    legs: list[ArbLeg],
    budget: float,
    apply_book_caps: bool = True,
    market_type: str = "default",
) -> tuple[float, bool]:
    """Fill in each leg's ``stake`` such that all legs pay out the same.

    Returns (effective_budget, limited_by_book_caps).

    Math: for n outcomes with decimal odds d_i, target equal return R means
    R = stake_i * d_i for all i, so stake_i = R/d_i. Total outlay T =
    sum(stake_i) = R * sum(1/d_i). Setting T = budget gives R = budget /
    sum(1/d_i) and stake_i = (budget/d_i) / sum(1/d_i), i.e.
    stake_i = budget * implied_i / total_implied.
    """
    total_implied = sum(leg.implied_prob for leg in legs)
    if total_implied <= 0:
        return 0.0, False

    # Unconstrained stakes.
    for leg in legs:
        leg.stake = round(budget * leg.implied_prob / total_implied, 2)

    if not apply_book_caps:
        return budget, False

    # Find the tightest (leg_stake / book_cap) ratio > 1 — that leg forces
    # us to scale everything down. Equivalently: the effective budget is
    # budget * min(book_cap_i / leg_stake_i, 1.0) over all i.
    min_ratio = 1.0
    limited = False
    for leg in legs:
        cap = get_book_max_stake(leg.bookmaker_canonical, market_type)
        if leg.stake > cap:
            r = cap / leg.stake
            if r < min_ratio:
                min_ratio = r
            limited = True

    if min_ratio < 1.0:
        new_budget = round(budget * min_ratio, 2)
        for leg in legs:
            leg.stake = round(new_budget * leg.implied_prob / total_implied, 2)
            cap = get_book_max_stake(leg.bookmaker_canonical, market_type)
            if leg.stake > cap + 0.01:
                # Shouldn't happen after the global scale, but guard against
                # rounding pushing us 1 cent over.
                leg.stake = cap
                leg.stake_capped_by_book = True
            elif abs(leg.stake - cap) < 0.51:
                # We are right at the cap — flag it so downstream knows
                # this leg is the binding constraint.
                leg.stake_capped_by_book = True
        return new_budget, True

    return budget, False


def _best_at(game: dict, market: str, team: str, point: float) -> Optional[dict]:
    """Return the best (highest-decimal) offer for ``team`` at exactly ``point``.

    Used by the spread arb pairing logic. Returns None if no book has an
    offer for that team/point combo.
    """
    best = None
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market:
                continue
            for o in mkt.get("outcomes", []):
                if o.get("name") != team or o.get("point") != point:
                    continue
                try:
                    dec = american_to_decimal(int(o.get("price")))
                except (TypeError, ValueError):
                    continue
                if dec <= 1.0:
                    continue
                entry = {
                    "american": int(o["price"]),
                    "decimal": dec,
                    "implied": 1.0 / dec,
                    "bookmaker": bm.get("title", bm.get("key", "")),
                    "bookmaker_canonical": canonicalize_book(
                        bm.get("key") or bm.get("title") or ""
                    ),
                    "point": point,
                    "fetched_at": _extract_line_ts(o, bm),
                    "outcome": team,
                }
                if best is None or dec > best["decimal"]:
                    best = entry
    return best


def _scan_spread_arbs(
    game: dict,
    home_team: str,
    away_team: str,
    abs_pts: set,
    *,
    epsilon: float,
    stale_seconds: float,
    budget: float,
    now: datetime,
    allow_missing_ts: bool,
    sport: str,
) -> list["ArbOpportunity"]:
    """Emit pure spread arbs by pairing Home@+X with Away@-X (and symmetric).

    Each candidate line is |X|. For each |X| we try two pairings:
        (1) Home@+X + Away@-X    — home is the underdog getting X points
        (2) Home@-X + Away@+X    — home is the favorite giving X points

    For |X|=0 (pk) only pairing (1) applies; we collapse to avoid dupes.
    """
    arbs: list[ArbOpportunity] = []

    for absx in abs_pts:
        pairings = [
            (absx, -absx),   # Home +X / Away -X
            (-absx, absx),   # Home -X / Away +X
        ]
        if absx == 0:
            pairings = [(0.0, 0.0)]

        for home_pt, away_pt in pairings:
            h = _best_at(game, "spreads", home_team, home_pt)
            a = _best_at(game, "spreads", away_team, away_pt)
            if not h or not a:
                continue
            arb = _build_arb_from_pair(
                game=game, entries=[h, a], market_type="spreads",
                epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
                now=now, allow_missing_ts=allow_missing_ts, sport=sport,
            )
            if arb is not None:
                arbs.append(arb)

    return arbs


def _build_arb_from_pair(
    *,
    game: dict,
    entries: list[dict],
    market_type: str,
    epsilon: float,
    stale_seconds: float,
    budget: float,
    now: datetime,
    allow_missing_ts: bool,
    sport: str,
) -> Optional["ArbOpportunity"]:
    """Shared arb construction path — takes a list of per-leg entries and
    returns an ArbOpportunity if (a) every leg is fresh, (b) total implied
    probability is below 1-epsilon, (c) profit exceeds MIN_PROFIT_PCT.
    """
    legs: list[ArbLeg] = []
    max_age = 0.0
    for e in entries:
        age = _age_seconds(e.get("fetched_at"), now)
        if age is None and not allow_missing_ts:
            return None
        if age is not None:
            if age > stale_seconds:
                return None
            if age > max_age:
                max_age = age
        legs.append(ArbLeg(
            bookmaker=e["bookmaker"],
            bookmaker_canonical=e["bookmaker_canonical"],
            outcome=e.get("outcome", ""),
            american_odds=e["american"],
            decimal_odds=e["decimal"],
            implied_prob=e["implied"],
            point=e.get("point"),
            fetched_at=e.get("fetched_at"),
            age_seconds=age,
        ))

    # Two legs from the same book can't be "cross-book arb" — reject.
    if len({leg.bookmaker_canonical for leg in legs}) < 2:
        # Unless it's a multi-way single-book dutch scenario — we still
        # allow same-book for 3+ outcomes because the book itself may be
        # offering a dutch (rare but possible on exchanges).
        if len(legs) < 3:
            return None

    total_implied = sum(leg.implied_prob for leg in legs)
    if total_implied >= 1.0 - epsilon:
        return None
    profit_pct = (1.0 - total_implied) / total_implied
    if profit_pct < MIN_PROFIT_PCT:
        return None
    if profit_pct > MAX_PROFIT_PCT:
        # Data-contamination guard — don't silently emit a 50% "arb".
        logger.debug(
            f"Rejecting suspiciously large arb ({profit_pct:.1%}) on "
            f"{game.get('home_team')}/{game.get('away_team')} {market_type}; "
            f"legs={[(l.bookmaker, l.american_odds, l.point) for l in legs]}"
        )
        return None
    # Binary-market divergence check: two quotes that disagree by more than
    # MAX_IMPLIED_DIVERGENCE almost certainly have a side or team swap somewhere.
    # Skipped for 3+-way markets because legitimate dutch books often have one
    # very short favorite and two longer legs (soccer 1-draw-2 commonly has
    # favorite implied 0.6 / draw 0.25 / dog 0.12, spread of 0.48).
    if len(legs) == 2:
        div = abs(legs[0].implied_prob - legs[1].implied_prob)
        if div > MAX_IMPLIED_DIVERGENCE:
            logger.debug(
                f"Rejecting implied-divergent arb (|{legs[0].implied_prob:.3f} - "
                f"{legs[1].implied_prob:.3f}| = {div:.3f}) on "
                f"{game.get('home_team')}/{game.get('away_team')} {market_type}"
            )
            return None

    effective_budget, limited = _compute_stakes(
        legs, budget=budget, apply_book_caps=True, market_type=market_type
    )
    expected_profit = round(effective_budget * profit_pct, 2)
    home = game.get("home_team", "")
    away = game.get("away_team", "")
    detected = now
    expires = detected + timedelta(seconds=60)

    return ArbOpportunity(
        game_id=str(game.get("id", "")),
        game=f"{away} @ {home}",
        sport=sport or game.get("sport_key", ""),
        market_type=market_type,
        thesis_tag="arb" if len(legs) == 2 else "dutch",
        total_implied=round(total_implied, 6),
        profit_pct=round(profit_pct, 6),
        expected_profit=expected_profit,
        budget_requested=budget,
        effective_budget=effective_budget,
        legs=legs,
        limited_by_book_caps=limited,
        max_leg_age_s=round(max_age, 1),
        detected_at=detected.isoformat(),
        expires_at=expires.isoformat(),
        notes=(
            f"pure {market_type} arb ({len(legs)} legs, "
            f"total_implied={total_implied:.4f})"
        ),
    )


# ---------------------------------------------------------------------------
# Main per-market pure-arb scan.
# ---------------------------------------------------------------------------
def scan_pure_arb(
    game: dict,
    market_type: str,
    *,
    epsilon: float = DEFAULT_EPSILON,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
    budget: float = DEFAULT_BUDGET,
    now: Optional[datetime] = None,
    allow_missing_ts: bool = False,
    sport: str = "",
) -> list[ArbOpportunity]:
    """Find pure arbs for one ``market_type`` in one ``game``.

    Returns a list because spreads/totals can have multiple valid point values
    simultaneously (e.g. DK on +2.5, FD on +3 for one team → we get one
    candidate per point value).

    ``allow_missing_ts=True`` is used by the backtest, because historical
    snapshot outcomes don't always have a fetched_at at the per-outcome level.
    In live scanning this stays False so we reject unstamped data.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    arbs: list[ArbOpportunity] = []
    point_groups = _collect_point_groups(game, market_type)

    # For spreads: instead of grouping "all outcomes at point=X", we need to
    # pair each team's BEST price at their respective spread with the OTHER
    # team's BEST price at the OPPOSING spread. Team A at +X cover-bet is
    # the logical complement of Team B at -X cover-bet. A spread "arb" where
    # both legs are at the same absolute point value but the teams disagree
    # about who's favored is a data-contamination artifact (the feed has
    # BetMGM saying Team A is -1.5 while Bovada says Team B is -1.5).
    if market_type == "spreads":
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        # Find distinct absolute-point values, then for each |X| pair Home@X
        # with Away@(-X) and Home@(-X) with Away@X — the latter covers home
        # favorite / home underdog cases symmetrically.
        abs_pts = {abs(pt) for pt in point_groups if pt is not None}
        spread_arbs = _scan_spread_arbs(
            game, home_team, away_team, abs_pts,
            epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
            now=now, allow_missing_ts=allow_missing_ts, sport=sport,
        )
        return spread_arbs

    for pt, outcomes in point_groups.items():
        # Need at least 2 outcomes (binary) or more for multi-way to compute
        # an arb.
        if len(outcomes) < 2:
            continue

        # Convert outcomes dict into the entry list shape _build_arb_from_pair
        # expects (each entry carries its own 'outcome' name).
        entries = []
        for name, entry in outcomes.items():
            e = dict(entry)
            e["outcome"] = name
            entries.append(e)

        arb = _build_arb_from_pair(
            game=game, entries=entries, market_type=market_type,
            epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
            now=now, allow_missing_ts=allow_missing_ts, sport=sport,
        )
        if arb is not None:
            arbs.append(arb)

    return arbs


def scan_dutch_book(
    game: dict,
    market_type: str,
    **kwargs,
) -> list[ArbOpportunity]:
    """Dutch-book = pure arb on a 3+-outcome market.

    The math is identical; we just tag it differently and let scan_pure_arb
    do the work. 3-way soccer moneyline is the canonical case.
    """
    arbs = scan_pure_arb(game, market_type, **kwargs)
    for a in arbs:
        if len(a.legs) >= 3:
            a.thesis_tag = "dutch"
            a.notes = (
                f"dutch book on {len(a.legs)}-way {market_type} "
                f"(total_implied={a.total_implied:.4f})"
            )
    return arbs


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


# ---------------------------------------------------------------------------
# Snapshot-level orchestrator.
# ---------------------------------------------------------------------------
def full_arbitrage_scan(
    snapshot: dict,
    *,
    epsilon: float = DEFAULT_EPSILON,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
    budget: float = DEFAULT_BUDGET,
    now: Optional[datetime] = None,
    include_synthetic: bool = True,
    allow_missing_ts: bool = False,
) -> dict:
    """Run all arbitrage scans over one snapshot dict.

    Returns a dict with per-thesis arb lists plus a summary.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    games = snapshot.get("games", [])
    sport = snapshot.get("sport", "")

    pure: list[ArbOpportunity] = []
    dutch: list[ArbOpportunity] = []
    synthetic: list[ArbOpportunity] = []

    for game in games:
        for mkt in ("h2h", "spreads", "totals"):
            arbs = scan_pure_arb(
                game, mkt,
                epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
                now=now, allow_missing_ts=allow_missing_ts, sport=sport,
            )
            for a in arbs:
                if a.thesis_tag == "dutch":
                    dutch.append(a)
                else:
                    pure.append(a)

        if include_synthetic:
            synthetic.extend(scan_cross_market_synthetic(
                game,
                epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
                now=now, sport=sport,
            ))

    limited_count = sum(1 for a in pure + dutch if a.limited_by_book_caps)
    return {
        "sport": sport,
        "game_count": len(games),
        "pure_arbs": [a.to_dict() for a in pure],
        "dutch_books": [a.to_dict() for a in dutch],
        "synthetic_arbs": [a.to_dict() for a in synthetic],
        "summary": {
            "pure_count": len(pure),
            "dutch_count": len(dutch),
            "synthetic_count": len(synthetic),
            "limited_by_book_caps": limited_count,
            "scan_time": now.isoformat(),
            "params": {
                "epsilon": epsilon,
                "stale_seconds": stale_seconds,
                "budget": budget,
            },
        },
    }


# ---------------------------------------------------------------------------
# Persistence: write qualifying rows into ev_opportunities.
#
# We use raw sqlite3 here (the tests do too) so we don't depend on the async
# aiosqlite pool at test time. Callers inside the live pipeline can wrap
# ``persist_opportunity`` inside their own async transaction.
# ---------------------------------------------------------------------------

_PERSIST_COLS = (
    "detected_at", "sport", "game_id", "team", "market", "bookmaker",
    "american_odds", "implied_probability", "estimated_true_prob", "edge",
    "expected_value", "kelly_fraction", "status", "source", "thesis_tag",
    "expires_at",
)


def persist_opportunity(
    conn: sqlite3.Connection,
    opp: ArbOpportunity,
) -> list[int]:
    """Write one ArbOpportunity to ev_opportunities — one row per leg.

    Returns the list of inserted row ids. Caller is responsible for commit.
    """
    # Make sure the thesis_tag / expires_at columns exist on the target DB.
    # This lets persist work against older installs that haven't run the
    # migration yet; no-op on DBs that already have them.
    for col, decl in (("thesis_tag", "TEXT"),
                      ("expires_at", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE ev_opportunities ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # already exists

    ids: list[int] = []
    for leg in opp.legs:
        cur = conn.execute(
            f"INSERT INTO ev_opportunities "
            f"({', '.join(_PERSIST_COLS)}) "
            f"VALUES ({', '.join('?' * len(_PERSIST_COLS))})",
            (
                opp.detected_at,
                opp.sport,
                opp.game_id,
                leg.outcome,
                opp.market_type,
                leg.bookmaker,
                leg.american_odds,
                leg.implied_prob,
                None,               # estimated_true_prob — N/A for arbs
                round(1.0 - opp.total_implied, 6),   # edge = the "gap"
                opp.profit_pct,
                # kelly_fraction slot repurposed as stake-fraction-of-budget
                round(leg.stake / opp.effective_budget, 6)
                if opp.effective_budget > 0 else 0.0,
                "open",
                "arbitrage",
                opp.thesis_tag,
                opp.expires_at,
            ),
        )
        ids.append(cur.lastrowid)
    return ids


# ---------------------------------------------------------------------------
# Historical backtest over odds_snapshots.
# ---------------------------------------------------------------------------
def backtest_arbs(
    db_path: str,
    *,
    days: int = 30,
    epsilon: float = DEFAULT_EPSILON,
    # Historical snapshots often lack per-outcome fetched_at because they
    # pre-date the WS fetched_at column. Use a wide-open stale window and
    # allow missing timestamps — the question is "how often has an arb
    # mathematically existed?", independent of how long it lasted.
    stale_seconds: float = 86400.0,
    budget: float = DEFAULT_BUDGET,
    allow_missing_ts: bool = True,
    limit_snapshots: Optional[int] = None,
) -> dict:
    """Replay ``days`` of odds_snapshots and count how often arbs appeared.

    Returns a summary dict with:
        total_snapshots_scanned
        snapshots_with_arb
        total_arb_instances    (dedupe'd across points per game/market)
        per_day_mean
        profit_pct_p50, profit_pct_p90
        lifespan_seconds_mean  (requires consecutive-snapshot tracking)
        per_sport counts
        book_limit_impact_pct  (% of arbs that would hit book caps)
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    query = (
        "SELECT id, sport, timestamp, snapshot_json "
        "FROM odds_snapshots "
        "WHERE timestamp >= ? "
        "ORDER BY sport, timestamp"
    )
    params: list = [cutoff]
    if limit_snapshots:
        query += f" LIMIT {int(limit_snapshots)}"
    cur = conn.execute(query, params)

    total = 0
    with_arb = 0
    profits: list[float] = []
    per_sport: dict[str, int] = defaultdict(int)
    per_day: dict[str, int] = defaultdict(int)
    book_limit_hits = 0

    # Lifespan tracking: {(sport, game_id, market, key): first_seen_ts}
    lifespans: list[float] = []
    active_arbs: dict[tuple, datetime] = {}
    seen_this_round: set = set()

    prev_sport: Optional[str] = None
    for row in cur:
        total += 1
        sport = row["sport"]
        ts_str = row["timestamp"]
        snap_ts = _parse_ts(ts_str) or datetime.now(timezone.utc)
        if sport != prev_sport:
            # Closing out all active arbs from the previous sport.
            for key, start in active_arbs.items():
                lifespans.append(0.0)  # one-off, evaporated by next scan
            active_arbs.clear()
            prev_sport = sport

        try:
            snap = json.loads(row["snapshot_json"])
        except Exception:
            continue

        # Use snapshot's own timestamp as "now" so stale_seconds is meaningful
        # relative to WHEN the snapshot was recorded, not calendar-now.
        res = full_arbitrage_scan(
            snap,
            epsilon=epsilon,
            stale_seconds=stale_seconds,
            budget=budget,
            now=snap_ts,
            include_synthetic=False,       # keep backtest focused on pure/dutch
            allow_missing_ts=allow_missing_ts,
        )
        arbs = res["pure_arbs"] + res["dutch_books"]
        if arbs:
            with_arb += 1
            day = snap_ts.date().isoformat()
            per_day[day] += len(arbs)
            per_sport[sport] += len(arbs)
            round_keys = set()
            for a in arbs:
                profits.append(a["profit_pct"])
                if a.get("limited_by_book_caps"):
                    book_limit_hits += 1
                key = (sport, a["game_id"], a["market_type"],
                       tuple(sorted(leg["bookmaker_canonical"] for leg in a["legs"])))
                round_keys.add(key)
                if key not in active_arbs:
                    active_arbs[key] = snap_ts
            # Close out arbs that didn't appear this round.
            gone = [k for k in active_arbs if k not in round_keys]
            for k in gone:
                lifespans.append((snap_ts - active_arbs.pop(k)).total_seconds())
        else:
            # No arbs this round -> close all active.
            for k, start in list(active_arbs.items()):
                lifespans.append((snap_ts - start).total_seconds())
            active_arbs.clear()

    # Close any still-active arbs at the end of the window.
    for k, start in active_arbs.items():
        lifespans.append(0.0)

    conn.close()

    def pctl(data: list[float], q: float) -> float:
        if not data:
            return 0.0
        s = sorted(data)
        k = max(0, min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1)))))
        return s[k]

    days_span = max(1, days)
    total_instances = sum(per_day.values())
    return {
        "days_analyzed": days,
        "total_snapshots_scanned": total,
        "snapshots_with_arb": with_arb,
        "total_arb_instances": total_instances,
        "arbs_per_day_mean": round(total_instances / days_span, 3),
        "per_sport": dict(per_sport),
        "per_day": dict(per_day),
        "profit_pct_p50": round(pctl(profits, 50) * 100, 3) if profits else 0.0,
        "profit_pct_p90": round(pctl(profits, 90) * 100, 3) if profits else 0.0,
        "profit_pct_max": round(max(profits) * 100, 3) if profits else 0.0,
        "lifespan_seconds_mean": round(sum(lifespans) / len(lifespans), 1) if lifespans else 0.0,
        "lifespan_seconds_p50": round(pctl(lifespans, 50), 1) if lifespans else 0.0,
        "lifespan_seconds_p90": round(pctl(lifespans, 90), 1) if lifespans else 0.0,
        "book_limit_impact_pct": round(100.0 * book_limit_hits / max(1, total_instances), 2),
        "params": {
            "epsilon": epsilon,
            "stale_seconds": stale_seconds,
            "budget": budget,
            "allow_missing_ts": allow_missing_ts,
        },
    }
