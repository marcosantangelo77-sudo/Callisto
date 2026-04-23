"""
Same-Game Parlay (SGP) edge scanner.

Books commonly price SGPs as if legs were independent (product of implied
probabilities), then layer on a flat "SGP multiplier" that may or may not
reflect the real pairwise correlations. This scanner:

  1. Enumerates candidate 2-leg and 3-leg SGPs for a game, drawn from three
     buckets:
        a. Moneyline x Total  — same-team ML + team/game total
        b. Player prop x Player prop — same or same-team players whose stats
           flow through a shared game script
        c. Team total x Opponent total — correlated by pace for some matchups
  2. For each candidate, computes a "theoretical fair price" using a Gaussian
     copula with per-sport calibrated correlations (from
     ``tools.sgp_correlations`` — seeded defaults, YAML overrides, or empirical
     calibration).
  3. If the *book's* quoted SGP price is better (longer American, higher
     decimal) than the theoretical fair by more than ``threshold``, emits an
     ``SGPEdge`` record.

The scanner is advisory only. It does NOT write to ``ev_opportunities`` —
integration is deliberately punted to a later agent. All it does is return a
list of SGPEdge dataclasses; the CLI wraps that for stdout.

Upstream-call discipline
------------------------
Per the task brief, we prefer pulling odds that the rest of Callisto already
ingested into ``odds_snapshots`` over making new upstream calls. The scanner
accepts pre-fetched ``game_odds`` (dict in the odds-api.io shape) and
optionally a ``fetch_book_sgp`` callable. It never opens an HTTP connection
itself. If callers need one, they can wire up their own @tracked_ingestion
fetcher that reads from DK's sgp API or odds-api.io and pass the callable in.

This keeps the scanner testable, credit-friendly, and free of ingestion
side-effects when run against the live DB.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Callable, Iterable, Optional

from tools.book_keys import canonicalize_book
from tools.sgp_correlations import get_correlation, get_source
# tools.sgp already implements a pure-Python Gaussian copula with a bivariate
# normal CDF. We reuse that rather than re-implement — the task brief
# explicitly asks us to READ from the existing correlation module without
# modifying it, and sgp.correlated_parlay_prob is the correct primitive.
from tools.sgp import correlated_parlay_prob

logger = logging.getLogger("callisto.sgp_scanner")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SGPLeg:
    """A single leg of an SGP candidate.

    Attributes
    ----------
    leg_type : str
        The canonical archetype (e.g. ``"qb_pass_yds_over"``), used for
        correlation lookup in ``tools.sgp_correlations``.
    description : str
        Human-readable ("Patrick Mahomes OVER 275.5 Pass Yds").
    american_odds : int
        The single-leg American price quoted by the book.
    fair_prob : float
        Our estimate of the leg's true hit probability. If the caller doesn't
        have a model fair, pass the book's implied prob (the scanner will
        still flag SGP-level correlation mispricing, but leg-level edge
        contribution will be zero by construction).
    market : str, optional
        Raw market key ("player_pass_yds", "totals"). Kept for debugging.
    player : str, optional
    team : str, optional
    side : str, optional
        ``"over"`` / ``"under"`` / ``"win"`` / ``"cover"`` — useful for
        rendering but not required by the math.
    line : float, optional
    """

    leg_type: str
    description: str
    american_odds: int
    fair_prob: float
    market: str = ""
    player: str = ""
    team: str = ""
    side: str = ""
    line: Optional[float] = None


@dataclass
class SGPEdge:
    """An SGP the scanner flags as potentially mispriced.

    Attributes
    ----------
    event_id : str
    legs : list[SGPLeg]
    book : str
        Canonicalized book slug (e.g. ``"draftkings"``).
    book_price_american : int
        The book's quoted SGP price in American odds.
    theoretical_fair_american : int
        Our correlation-adjusted fair price.
    edge_pct : float
        ``(fair_prob - book_prob) / book_prob * 100``.
    correlation_assumed : float
        Average pairwise correlation used.
    confidence : str
        One of ``"high"``, ``"medium"``, ``"low"`` — based on correlation
        provenance and leg count (see ``_assess_confidence``).
    expires_at : str
        ISO-8601 UTC timestamp of the game's commence_time, if known.
    meta : dict
        Carries scanner diagnostics: per-pair correlations, source (seeded
        vs empirical), naive-joint prob, etc. Useful for CLI output.
    """

    event_id: str
    legs: list[SGPLeg]
    book: str
    book_price_american: int
    theoretical_fair_american: int
    edge_pct: float
    correlation_assumed: float
    confidence: str
    expires_at: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["legs"] = [asdict(l) for l in self.legs]
        return d


# ---------------------------------------------------------------------------
# Odds math helpers (kept local to avoid drag on large imports; mirrors
# tools.math_utils but we don't want a circular dep on tools.odds_api).
# ---------------------------------------------------------------------------

def _american_to_implied(odds: int) -> float:
    if odds == 0:
        return 0.0
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def _implied_to_american(prob: float) -> int:
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return int(round(-100 * prob / (1 - prob)))
    return int(round(100 * (1 - prob) / prob))


def _american_to_decimal(odds: int) -> float:
    if odds == 0:
        return 1.0
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / (-odds)


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------

def _same_team(a: SGPLeg, b: SGPLeg) -> bool:
    return bool(a.team and b.team and a.team == b.team)


def _is_player_leg(leg: SGPLeg) -> bool:
    return leg.leg_type.startswith(("qb_", "wr_", "rb_", "te_", "player_",
                                    "batter_", "pitcher_", "skater_",
                                    "goalie_"))


def _is_team_leg(leg: SGPLeg) -> bool:
    return leg.leg_type.startswith(("team_", "opp_", "game_"))


def _complementary_types(a: str, b: str) -> bool:
    """Over+Under of the same underlying stat — a structural no-op."""
    for over, under in (("_over", "_under"),):
        if a.endswith(over) and b.endswith(under) and a[: -len(over)] == b[: -len(under)]:
            return True
        if b.endswith(over) and a.endswith(under) and b[: -len(over)] == a[: -len(under)]:
            return True
    return False


def _has_complementary_pair(legs: Iterable[SGPLeg]) -> bool:
    ll = list(legs)
    for i, a in enumerate(ll):
        for b in ll[i + 1:]:
            if not _complementary_types(a.leg_type, b.leg_type):
                continue
            # Complementary types only matter when they reference the same
            # underlying event. Player+line match, or game-level match.
            same_player = (a.player and a.player == b.player) and (a.line == b.line)
            same_game_market = (
                a.market == b.market and a.line == b.line and not a.player and not b.player
            )
            if same_player or same_game_market:
                return True
    return False


def enumerate_candidates(
    legs: list[SGPLeg],
    *,
    min_legs: int = 2,
    max_legs: int = 3,
) -> list[tuple[SGPLeg, ...]]:
    """Enumerate SGP candidate combinations from an available-leg pool.

    Skips combos that are structurally degenerate:
      - Legs that reference the same market/side (would be identical).
      - 3+ legs from the exact same player (book collapses anyway).

    This is a pure function — no odds math, no correlation lookup. Gives the
    scanner a pre-filtered candidate set to evaluate.
    """
    candidates: list[tuple[SGPLeg, ...]] = []
    n = len(legs)
    for k in range(min_legs, max_legs + 1):
        if k > n:
            break
        for combo in combinations(range(n), k):
            selected = tuple(legs[i] for i in combo)
            # Skip duplicates. Two legs are "the same bet" if their identifying
            # tuple matches. When meta fields (player/market/side/line) are
            # empty we fall back to leg_type so synthetic test legs with only
            # leg_type populated aren't all collapsed into one fingerprint.
            def _fp(l: SGPLeg) -> tuple:
                if any((l.player, l.market, l.side, l.line is not None)):
                    return (l.player, l.market, l.side, l.line)
                return ("_byleg", l.leg_type)
            fingerprints = {_fp(l) for l in selected}
            if len(fingerprints) < len(selected):
                continue
            # Skip 3-leg combos entirely on one player
            players = {l.player for l in selected if l.player}
            if k >= 3 and len(players) == 1 and players != {""}:
                continue
            # Skip combos that contain complementary legs (over+under of the
            # same player/market/line, or Mahomes over AND Mahomes under).
            # Books don't accept these in an SGP anyway.
            if _has_complementary_pair(selected):
                continue
            candidates.append(selected)
    return candidates


# ---------------------------------------------------------------------------
# Core pricing
# ---------------------------------------------------------------------------

def _pairwise_correlation_matrix(
    sport: str, combo: Iterable[SGPLeg]
) -> tuple[list[list[float]], list[dict]]:
    """Build an NxN correlation matrix for a leg combo and a debug list."""
    legs = list(combo)
    n = len(legs)
    mat = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    pair_info: list[dict] = []
    for i, j in combinations(range(n), 2):
        rho = get_correlation(sport, legs[i].leg_type, legs[j].leg_type)
        # Same-team modifier: intra-team correlations are stronger than the
        # generic prior suggests (the prior is often averaged across team
        # assignments). If both legs are on the same team and the generic
        # prior is weak-positive, give it a small bump. This is conservative —
        # we never reduce correlations.
        if rho > 0 and _same_team(legs[i], legs[j]):
            rho = min(1.0, rho + 0.05)
        # Opposite-team scoring: team_total vs opp_total is negative for NFL
        # (blowout suppresses) but pace-positive for NBA. Already handled in
        # _DEFAULTS — we do not modify here.
        mat[i][j] = mat[j][i] = rho
        pair_info.append(
            {
                "leg_a": legs[i].leg_type,
                "leg_b": legs[j].leg_type,
                "rho": rho,
                "source": get_source(sport, legs[i].leg_type, legs[j].leg_type),
            }
        )
    return mat, pair_info


def theoretical_sgp_prob(
    sport: str,
    combo: Iterable[SGPLeg],
) -> tuple[float, float, list[dict]]:
    """Return ``(theoretical_joint_prob, naive_joint_prob, pair_info)``.

    ``theoretical`` uses the Gaussian copula with calibrated correlations.
    ``naive`` is the independent product — what a book charges when it
    ignores correlations. The gap between the two is the structural edge if
    it exceeds the book's SGP juice.
    """
    legs = list(combo)
    probs = [max(1e-6, min(1 - 1e-6, l.fair_prob)) for l in legs]
    naive = 1.0
    for p in probs:
        naive *= p
    mat, pair_info = _pairwise_correlation_matrix(sport, legs)
    try:
        theoretical = correlated_parlay_prob(probs, mat)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Gaussian copula failed, falling back to naive: %s", exc)
        theoretical = naive
    return theoretical, naive, pair_info


def _assess_confidence(pair_info: list[dict], n_legs: int) -> str:
    """Heuristic confidence label.

    - ``high``: all pairs have an empirical source and n_legs==2
    - ``medium``: at least one empirical pair, OR all pairs seeded and n_legs==2
    - ``low``: any missing pair, or n_legs>=3 with only seeded priors

    Rationale: 2-leg bivariate copulas are exact; 3-leg uses a pairwise
    approximation that's fine for moderate correlations but decays when
    legs interact (think QB+WR+TE, where WR and TE compete for targets).
    """
    sources = [p.get("source", "missing") for p in pair_info]
    any_missing = any(s == "missing" for s in sources)
    all_empirical = all(s == "empirical" for s in sources) if sources else False
    any_empirical = any(s == "empirical" for s in sources) if sources else False

    if any_missing:
        return "low"
    if all_empirical and n_legs == 2:
        return "high"
    if any_empirical or n_legs == 2:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Book-SGP pricing lookup
# ---------------------------------------------------------------------------

SGPFetcher = Callable[[str, str, str, list[SGPLeg]], Optional[int]]
"""Signature: (sport, event_id, book, legs) -> book's SGP price in American odds
or None if the book doesn't price this combo."""


def default_no_fetcher(
    sport: str, event_id: str, book: str, legs: list[SGPLeg]
) -> Optional[int]:
    """Default fetcher: no upstream call. Returns None so the scanner will
    instead use a *synthesized* book price = naive-independent with an
    assumed SGP juice multiplier. Callers who have a real DK endpoint can
    inject a better fetcher."""
    return None


def synthesize_book_sgp_price(
    legs: list[SGPLeg],
    sgp_juice_pct: float = 0.07,
) -> int:
    """Estimate what a book would quote for this SGP if all it does is apply
    a flat correlation multiplier to the independent product.

    This is what books that don't individually model correlations do — they
    multiply implied probabilities and then shave a flat percentage. Used as
    a fallback when we don't have a real SGP quote.

    ``sgp_juice_pct`` defaults to 7% — in line with DK/FD's observed SGP
    margins on typical 2-leg combos. Real juice varies; this is a coarse
    approximation. Flag with confidence='low' downstream.
    """
    naive = 1.0
    for leg in legs:
        naive *= _american_to_implied(leg.american_odds)
    # Book shaves its edge by bumping implied upward
    bumped = min(0.999, naive * (1.0 + sgp_juice_pct))
    return _implied_to_american(bumped)


# ---------------------------------------------------------------------------
# Public scanner API
# ---------------------------------------------------------------------------

def scan_sgp_edges(
    sport: str,
    event_id: str,
    legs: list[SGPLeg],
    *,
    book: str = "draftkings",
    fetch_book_sgp: SGPFetcher = default_no_fetcher,
    threshold: float = 0.03,
    min_legs: int = 2,
    max_legs: int = 3,
    expires_at: str = "",
    assumed_sgp_juice_pct: float = 0.07,
) -> list[SGPEdge]:
    """Scan a game's candidate SGPs and return the mispriced ones.

    Parameters
    ----------
    sport
        Sport key (``"nfl"``, ``"nba"``, ``"mlb"``, etc.). Whatever
        canonical form ``sgp_correlations`` recognizes; odds-api-style
        prefixes (``"baseball_mlb"``) are tolerated.
    event_id
        DK/odds-api event id — echoed into the output.
    legs
        Pre-assembled leg pool. The caller is responsible for turning raw
        odds into ``SGPLeg`` archetypes. See the companion helper
        :func:`legs_from_game_odds` for an automated adaptor.
    book
        Target book. Only used for canonicalization + book-specific fetcher.
    fetch_book_sgp
        Optional callable that returns the book's actual SGP American odds
        for a given leg combo. Defaults to ``default_no_fetcher`` which
        returns ``None``; in that case we fall back to a synthesized book
        price. Inject a real fetcher to get sharper edges.
    threshold
        Minimum edge fraction to flag. ``0.03`` = flag when our fair prob is
        at least 3% higher than the book's implied prob.
    min_legs, max_legs
        Combo size bounds. Defaults match the task brief (2 and 3 legs).
    expires_at
        Optional ISO-8601 timestamp (game commence time); echoed into each
        edge.
    assumed_sgp_juice_pct
        Used only when ``fetch_book_sgp`` returns ``None``. See
        :func:`synthesize_book_sgp_price`.
    """
    sport_key = _normalize_sport(sport)
    canonical_book = canonicalize_book(book)
    edges: list[SGPEdge] = []

    if not legs or len(legs) < 2:
        return edges

    candidates = enumerate_candidates(legs, min_legs=min_legs, max_legs=max_legs)
    logger.debug(
        "scan_sgp_edges: %s %s, %d legs -> %d candidate combos",
        sport_key, event_id, len(legs), len(candidates),
    )

    for combo in candidates:
        combo_list = list(combo)
        fair_prob, naive_prob, pair_info = theoretical_sgp_prob(sport_key, combo_list)

        book_odds = fetch_book_sgp(sport_key, event_id, canonical_book, combo_list)
        book_price_source = "fetched"
        if book_odds is None:
            book_odds = synthesize_book_sgp_price(combo_list, assumed_sgp_juice_pct)
            book_price_source = "synthesized"

        book_prob = _american_to_implied(book_odds)
        if book_prob <= 0:
            continue

        edge = fair_prob - book_prob
        edge_pct = (edge / book_prob) if book_prob > 0 else 0.0
        if edge_pct <= threshold:
            continue

        avg_rho = (
            sum(abs(p["rho"]) for p in pair_info) / len(pair_info)
            if pair_info else 0.0
        )
        # Directional average (signed) — needed so the CLI can show whether
        # the edge is positive-correlation driven vs anti-correlation.
        signed_avg_rho = (
            sum(p["rho"] for p in pair_info) / len(pair_info)
            if pair_info else 0.0
        )

        confidence = _assess_confidence(pair_info, len(combo_list))
        if book_price_source == "synthesized" and confidence == "high":
            confidence = "medium"

        edges.append(
            SGPEdge(
                event_id=event_id,
                legs=combo_list,
                book=canonical_book,
                book_price_american=int(book_odds),
                theoretical_fair_american=_implied_to_american(fair_prob),
                edge_pct=round(edge_pct * 100, 3),
                correlation_assumed=round(signed_avg_rho, 4),
                confidence=confidence,
                expires_at=expires_at,
                meta={
                    "fair_prob": round(fair_prob, 6),
                    "naive_prob": round(naive_prob, 6),
                    "book_prob": round(book_prob, 6),
                    "abs_avg_correlation": round(avg_rho, 4),
                    "book_price_source": book_price_source,
                    "pair_correlations": pair_info,
                    "sport_key": sport_key,
                    "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
            )
        )

    edges.sort(key=lambda e: e.edge_pct, reverse=True)
    return edges


def _normalize_sport(sport: str) -> str:
    s = (sport or "").strip().lower()
    for prefix in (
        "americanfootball_",
        "basketball_",
        "baseball_",
        "icehockey_",
    ):
        if s.startswith(prefix):
            return s[len(prefix):]
    # Explicit aliases for league-only calls
    aliases = {
        "nfl": "nfl",
        "ncaaf": "nfl",
        "nba": "nba",
        "ncaab": "nba",
        "wnba": "wnba",
        "mlb": "mlb",
        "nhl": "nhl",
    }
    return aliases.get(s, s)


# ---------------------------------------------------------------------------
# Adapter: build legs from odds-api.io game-odds payload
# ---------------------------------------------------------------------------

def _leg_type_from_market(market_key: str, side: str, is_favorite: bool) -> Optional[str]:
    """Best-effort mapping from odds-api market key to canonical archetype."""
    side = (side or "").lower()
    mk = (market_key or "").lower()
    if mk == "h2h":
        return "team_ml_win" if is_favorite else None  # dogs rarely anchor SGPs
    if mk == "spreads":
        return "team_spread_cover"
    if mk == "totals":
        return f"game_total_{'over' if side == 'over' else 'under'}"
    if mk == "team_totals":
        return f"team_total_{'over' if side == 'over' else 'under'}"
    # Player props — partial coverage; scanner ignores unmapped markets
    prop_map = {
        "player_pass_yds": f"qb_pass_yds_{side}",
        "player_pass_tds": f"qb_pass_tds_{side}",
        "player_rush_yds": f"rb_rush_yds_{side}",
        "player_receiving_yds": f"wr_rec_yds_{side}",
        "player_receptions": f"wr_rec_{side}",
        "player_points": f"player_pts_{side}",
        "player_assists": f"player_ast_{side}",
        "player_rebounds": f"player_reb_{side}",
        "player_threes": f"player_threes_{side}",
        "batter_hits": f"batter_hits_{side}",
        "batter_total_bases": f"batter_tb_{side}",
        "batter_rbi": f"batter_rbi_{side}",
        "batter_home_runs": f"batter_hr_{side}",
        "pitcher_strikeouts": f"pitcher_ks_{side}",
    }
    return prop_map.get(mk)


def legs_from_game_odds(
    game: dict,
    *,
    book_priority: Iterable[str] = ("draftkings", "fanduel", "betmgm"),
    include_player_props: bool = True,
) -> list[SGPLeg]:
    """Adapt an odds-api.io game dict into a pool of SGPLegs.

    This is a minimal best-effort adaptor — only standard markets, plus a
    small hand-curated set of player props. Good enough for the CLI + the
    dry-run survey. Returns an empty list if no markets are usable.
    """
    bookmakers = game.get("bookmakers") or []
    if not bookmakers:
        return []

    # Pick the first available book in priority order
    canonical_priority = [canonicalize_book(b) for b in book_priority]
    chosen = None
    for bm in bookmakers:
        if canonicalize_book(bm.get("key") or bm.get("title")) in canonical_priority:
            chosen = bm
            break
    if chosen is None:
        chosen = bookmakers[0]

    home = game.get("home_team", "")
    away = game.get("away_team", "")
    legs: list[SGPLeg] = []

    for mkt in chosen.get("markets", []) or []:
        mk = (mkt.get("key") or "").lower()
        if not include_player_props and mk.startswith(("player_", "batter_", "pitcher_")):
            continue
        outcomes = mkt.get("outcomes") or []
        # Determine favorite (for h2h)
        fav_team = ""
        if mk == "h2h":
            sorted_out = sorted(outcomes, key=lambda o: o.get("price", 0))
            if sorted_out:
                fav_team = sorted_out[0].get("name", "")
        for o in outcomes:
            name = o.get("name", "")
            side = "over" if name.lower() == "over" else (
                "under" if name.lower() == "under" else "win"
            )
            is_favorite = (name == fav_team)
            leg_type = _leg_type_from_market(mk, side, is_favorite)
            if not leg_type:
                continue
            price = int(o.get("price", 0))
            if price == 0:
                continue
            team = name if mk in ("h2h", "spreads") else (o.get("team") or "")
            player = o.get("description", "") or o.get("player_name", "") or ""
            legs.append(
                SGPLeg(
                    leg_type=leg_type,
                    description=o.get("description") or f"{name} {mk}",
                    american_odds=price,
                    fair_prob=_american_to_implied(price),
                    market=mk,
                    player=player,
                    team=team or ("home" if name == home else "away" if name == away else ""),
                    side=side,
                    line=o.get("point"),
                )
            )
    return legs
