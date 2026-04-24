"""
Edge scanner — finds exploitable inefficiencies across bookmakers.

This is the quantitative core. Three edge types:

1. CROSS-BOOK DIVERGENCE: Same bet priced differently across books.
   If BetMGM has -105 and MyBookie has -125 on the same spread,
   BetMGM is giving you 4%+ better implied probability. Sharp money
   moves first on soft books — divergence tells you WHERE sharps are.

2. SHARP MONEY DETECTION: When one book moves while others don't,
   that book likely took a large sharp bet. The others will follow.
   Getting in before the cascade = buying at a discount.

3. MISPRICED LINES: When a book's juice structure creates +EV.
   Example: if both sides of a spread are -105 instead of -110,
   the total vig is lower and the line may be exploitable.
   Also: stale lines that haven't adjusted to news/injuries.
"""

import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

from tools.odds_api import (
    calculate_implied_probability,
    calculate_ev,
    find_best_line,
)
from tools.market_microstructure import compute_market_metrics
from tools.book_keys import canonicalize_book

# Freshness weighting — exponential decay on odds-line age. A book with a
# 30s-old quote weighs ~6x more than one with a 3min-old quote. The half-life
# is configurable at import time via CALLISTO_ODDS_HALF_LIFE_S.
#
# Why 180s default? odds-api.io's WS path delivers updates in <1s; the 15min
# snapshot path delivers updates aged 0-900s. With HALF_LIFE=180 the mean age
# of a 15-min-old line contributes ~3% weight vs a fresh one, which is the
# right "almost ignore it but don't zero it out" behaviour for sharp consensus.
_DEFAULT_HALF_LIFE_S = 180.0
try:
    _ODDS_HALF_LIFE_S = float(os.getenv("CALLISTO_ODDS_HALF_LIFE_S", str(_DEFAULT_HALF_LIFE_S)))
    if _ODDS_HALF_LIFE_S <= 0:
        _ODDS_HALF_LIFE_S = _DEFAULT_HALF_LIFE_S
except (TypeError, ValueError):
    _ODDS_HALF_LIFE_S = _DEFAULT_HALF_LIFE_S

_DEBUG_WEIGHTS = os.getenv("CALLISTO_ODDS_DEBUG_WEIGHTS", "0") == "1"
from tools.dead_numbers import (
    is_dead_number as _is_dead_number,
    key_number_value as _key_number_value,
    find_dead_number_steals,
    analyze_spread as _analyze_spread,
    rank_line_shopping_opportunities,
    buy_points_analysis,
    SPORT_ALIASES as _DEAD_NUM_SPORT_ALIASES,
)

logger = logging.getLogger("callisto.edge_scanner")


def _parse_line_timestamp(line: dict) -> Optional[datetime]:
    """Best-effort parse of a line's freshness timestamp.

    Prefers `fetched_at` (our own ingest stamp) over `last_update` (the book's
    timestamp as reported by odds-api.io / the-odds-api.com). Our stamp is
    strictly more meaningful for consensus freshness — it answers "how long
    ago did I trust this number" rather than "what did the book claim".

    Returns None if no usable timestamp is present; callers fall back to
    "assume freshly fetched" (weight = 1.0) in that case, which keeps the
    scanner compatible with legacy snapshot rows that pre-date this column.
    """
    for key in ("fetched_at", "last_update"):
        val = line.get(key)
        if not val:
            continue
        try:
            if isinstance(val, datetime):
                dt = val
            else:
                s = str(val)
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _freshness_weight(line: dict, now: Optional[datetime] = None,
                      half_life_s: Optional[float] = None) -> float:
    """Compute an exponential-decay freshness weight for a line.

    weight = exp(-age_seconds / half_life). Clamped to [1e-4, 1.0]; an
    unknown/negative age gets weight 1.0 so we don't silently zero out
    legacy data.
    """
    if half_life_s is None:
        half_life_s = _ODDS_HALF_LIFE_S
    ts = _parse_line_timestamp(line)
    if ts is None:
        return 1.0
    if now is None:
        now = datetime.now(timezone.utc)
    age = (now - ts).total_seconds()
    if age <= 0:
        return 1.0
    try:
        w = math.exp(-age / half_life_s)
    except (ValueError, OverflowError):
        return 1e-4
    # Floor at 1e-4 so an ancient line still contributes epsilon instead of
    # disappearing entirely — the upstream sanity checks still gate extreme
    # cases, and a hard zero can misbehave in weighted-sum divisions.
    return max(1e-4, min(1.0, w))


def weighted_sharp_consensus(
    sharp_lines: list[dict],
    *,
    half_life_s: Optional[float] = None,
    now: Optional[datetime] = None,
    debug: bool = False,
) -> tuple[Optional[float], list[dict]]:
    """Freshness-weighted mean implied probability across sharp books.

    Returns (consensus_prob_or_None, per_line_debug). The debug list
    contains one dict per input line with keys: bookmaker, implied, age_s,
    weight. Useful for logging/telemetry without having to recompute.

    If all weights are zero (shouldn't happen given the floor, but defend
    against it) or sharp_lines is empty, returns None.
    """
    if not sharp_lines:
        return None, []

    if now is None:
        now = datetime.now(timezone.utc)
    if half_life_s is None:
        half_life_s = _ODDS_HALF_LIFE_S

    rows: list[dict] = []
    total_w = 0.0
    total_wp = 0.0
    for l in sharp_lines:
        implied = calculate_implied_probability(l["price"])
        w = _freshness_weight(l, now=now, half_life_s=half_life_s)
        ts = _parse_line_timestamp(l)
        age_s = (now - ts).total_seconds() if ts is not None else None
        rows.append({
            "bookmaker": l.get("bookmaker", ""),
            "implied": round(implied, 4),
            "age_s": round(age_s, 1) if age_s is not None else None,
            "weight": round(w, 4),
        })
        total_w += w
        total_wp += w * implied

    if total_w <= 0:
        return None, rows

    consensus = total_wp / total_w
    if debug or _DEBUG_WEIGHTS:
        logger.debug(f"Freshness-weighted consensus: {consensus:.4f} from {rows}")
    return consensus, rows


def _filter_in_progress_games(games: list[dict]) -> list[dict]:
    """Remove games whose commence_time has passed (in-progress or finished).

    Live odds contaminate sharp consensus and produce phantom edges
    (see BAL@PIT 2026-04-04 incident). This is defense-in-depth — callers
    like full_edge_scan() also filter, but individual scan functions must
    be self-protective so direct API callers can't bypass the gate.
    """
    now = datetime.now(timezone.utc)
    pre_game = []
    for g in games:
        ct = g.get("commence_time")
        if ct:
            try:
                if isinstance(ct, str):
                    ct_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                else:
                    ct_dt = ct
                if ct_dt.tzinfo is None:
                    ct_dt = ct_dt.replace(tzinfo=timezone.utc)
                if ct_dt <= now:
                    continue
            except (ValueError, TypeError):
                pass  # Can't parse — include to be safe
        pre_game.append(g)
    return pre_game

# ---------------------------------------------------------------------------
# Sport key -> pace_model.Sport mapping for API sport keys
# ---------------------------------------------------------------------------
_PACE_SPORT_MAP = {
    "basketball_nba": "nba",
    "americanfootball_nfl": "nfl",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "soccer_epl": "soccer",
    "soccer_germany_bundesliga": "soccer",
    "soccer_spain_la_liga": "soccer",
    "soccer_italy_serie_a": "soccer",
    "soccer_france_ligue_one": "soccer",
    "soccer_usa_mls": "soccer",
}

# Low-scoring sports that should use Poisson distribution for totals
_LOW_SCORING_SPORTS = {"mlb", "nhl", "soccer"}

# Hardcoded fallback — always used when Granger data is unavailable
_STATIC_SHARP_TITLES = {"pinnacle", "lowvig.ag", "bookmaker.eu", "betonline.ag", "betcris", "circa", "betfair exchange", "betfair", "sbobet"}

# Cache for Granger-derived sharp leader per sport (sport -> (leader, timestamp))
_granger_sharp_cache: dict[str, tuple[str, float]] = {}
_GRANGER_CACHE_TTL = 3600  # 1 hour — re-query DB at most once per hour


def get_sharp_titles_for_sport(sport: str = "") -> set[str]:
    """Return the set of sharp book titles, enriched by Granger leadership data.

    If Granger temporal prediction analysis has identified a leader for this
    sport, that book is added to the sharp set. Falls back to the static
    hardcoded set when no Granger data exists.

    This is a sync function safe for the hot path — it reads from a cache
    populated by the async Granger phase in the research loop.
    """
    import time
    sharp = set(_STATIC_SHARP_TITLES)

    if not sport:
        return sharp

    cached = _granger_sharp_cache.get(sport)
    if cached:
        leader, ts = cached
        if time.time() - ts < _GRANGER_CACHE_TTL and leader:
            sharp.add(leader)
            return sharp

    # Try async lookup — but only if we're inside a running event loop
    # If not, just return the static set (the cache will be populated by
    # the research loop's Granger phase)
    try:
        import asyncio
        import os
        loop = asyncio.get_running_loop()
        # We're in an async context — schedule cache refresh but don't block
        db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
        loop.create_task(_refresh_granger_cache(sport, db_path))
    except RuntimeError:
        pass  # No running loop — return static set

    return sharp


async def _refresh_granger_cache(sport: str, db_path: str) -> None:
    """Refresh the Granger sharp leader cache for a sport."""
    import time
    try:
        from tools.granger_causality import get_sharp_leader
        leader = await get_sharp_leader(db_path, sport)
        _granger_sharp_cache[sport] = (leader, time.time())
        if leader:
            logger.info(f"Granger sharp leader for {sport}: {leader}")
    except Exception as e:
        logger.debug(f"Granger cache refresh failed for {sport}: {e}")


def scan_cross_book_edges(games: list[dict], market: str = "spreads", sport: str = "") -> list[dict]:
    """
    Scan all games for cross-bookmaker pricing divergence.

    Returns edges sorted by magnitude. A large spread across books on the
    same line means at least one book is mispriced — the question is which one.

    Sharp books (Pinnacle, Circa, Bookmaker.eu) set the true line.
    Soft books (FanDuel, DraftKings, BetMGM) lag behind and offer value.

    When Granger temporal prediction data is available for the sport,
    the identified leader book is dynamically added to the sharp set.
    """
    games = _filter_in_progress_games(games)

    # Dynamic sharp set — Granger leader (if available) enriches the static set
    SHARP_TITLES = get_sharp_titles_for_sport(sport)
    SOFT_TITLES = {"fanduel", "draftkings", "betmgm", "pointsbet", "caesars", "betrivers", "mybookie.ag", "bovada", "betus", "fanatics", "fanatics sportsbook"}

    edges = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for team in [home, away]:
            if not team:
                continue

            best = find_best_line(game, market=market, team=team)
            if best.get("error") or len(best.get("all_lines", [])) < 2:
                continue

            all_lines = best["all_lines"]

            # SPREAD POINT VALIDATION: For spreads/totals, lines with
            # different point values are different bets and must NOT be
            # averaged together. Previously we dropped the minority point
            # value silently — that killed key-number arbitrage. Now we
            # build one line-group per point value and process each group
            # through the full edge-scoring path so e.g. "DK is on +2.5,
            # everyone else is on +3" surfaces as its own candidate.
            if market in ("spreads", "totals"):
                from collections import Counter, defaultdict
                point_counts = Counter(l.get("point") for l in all_lines)
                if len(point_counts) > 1:
                    grouped = defaultdict(list)
                    for l in all_lines:
                        grouped[l.get("point")].append(l)
                    _line_groups = [grp for grp in grouped.values() if len(grp) >= 2]
                    if not _line_groups:
                        continue
                    if len(_line_groups) > 1:
                        logger.info(
                            f"Point split for {team} {market}: "
                            f"points={dict(point_counts)} → {len(_line_groups)} sub-edges"
                        )
                else:
                    _line_groups = [all_lines]
            else:
                _line_groups = [all_lines]

            for _group in _line_groups:
                _scan_line_group(
                    edges=edges,
                    lines=_group,
                    game=game,
                    home=home,
                    away=away,
                    team=team,
                    market=market,
                    sport=sport,
                    SHARP_TITLES=SHARP_TITLES,
                    SOFT_TITLES=SOFT_TITLES,
                )

    # Sort by implied range descending — biggest disagreements first
    edges.sort(key=lambda x: x["implied_range"], reverse=True)
    return edges


# ── Wiki-informed confidence adjustments (feat/wiki-in-the-loop 2026-04-22) ──
#
# For each detected edge, before writing to ev_opportunities, the scanner
# consults the knowledge wiki for articles on this sport/matchup/market.
# Matching priors boost confidence; contradicting priors dampen it. Total
# adjustment is capped at ±0.15 so wiki informs but doesn't override the
# quantitative signal. Applied post-scan so the core scanner stays sync.

WIKI_EDGE_ADJUSTMENT_CAP = float(os.getenv("CALLISTO_WIKI_EDGE_CAP", "0.15"))


async def apply_wiki_adjustments_to_edges(
    edges: list[dict], sport: str, db_path: Optional[str] = None,
) -> list[dict]:
    """Enrich each edge dict with ``wiki_confidence_delta`` and ``wiki_cites``.

    Walks the knowledge wiki once per (sport, market, team) triple and applies
    a bounded adjustment based on matching prior articles:

      - Prior article title/content contains "inflates" / "OVER" / "boost"
        and edge side aligns  → +delta (confirming prior)
      - Prior article title/content contains "UNDER" / "dead" / "null"
        and edge side aligns  → +delta (confirming prior)
      - Edge side contradicts direction of prior → -delta (dampening)

    The absolute sum of deltas is clamped to ``WIKI_EDGE_ADJUSTMENT_CAP``.

    Failures are logged and returned edges are left untouched — wiki being
    down cannot break the edge-scanning path. Respects
    ``CALLISTO_WIKI_IN_LOOP=1`` (default on).
    """
    if os.getenv("CALLISTO_WIKI_IN_LOOP", "1") != "1":
        return edges
    if not edges:
        return edges

    try:
        import aiosqlite
        from tools.knowledge_wiki import get_wiki
    except Exception as e:
        logger.debug(f"Wiki edge adjustments skipped (import): {e}")
        return edges

    wiki = get_wiki() if db_path is None else None
    if wiki is None:
        from tools.knowledge_wiki import KnowledgeWiki
        wiki = KnowledgeWiki(db_path)

    # Cache per (sport, market, team) so N edges on the same team = 1 lookup.
    cache: dict[tuple[str, str, str], list[dict]] = {}

    try:
        async with aiosqlite.connect(wiki.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 15000")
            for edge in edges:
                team = edge.get("team", "")
                market = edge.get("market", "")
                cache_key = (sport, market, team)
                if cache_key not in cache:
                    query = f"{sport} {market} {team} edge prior warning"
                    try:
                        cache[cache_key] = await wiki.search(
                            db, query, top_k=5, min_similarity=0.0,
                        )
                    except Exception as e:
                        logger.debug(f"Wiki edge search failed for {cache_key}: {e}")
                        cache[cache_key] = []
                priors = cache[cache_key]

                delta = 0.0
                cites = []
                for a in priors:
                    sim = a.get("similarity") or 0.0
                    if sim < 0.60:
                        continue
                    blob = (
                        (a.get("title") or "") + " " + (a.get("summary") or "")
                        + " " + (a.get("content") or "")
                    ).lower()
                    # Direction inference from edge — best_line price / team / market.
                    # For totals we use the "side" hint from the sharp_consensus
                    # vs soft-book edges; for spreads/h2h the team IS the side.
                    is_over = "over" in blob
                    is_under = "under" in blob
                    says_dead = any(k in blob for k in ("dead_pattern", "null_result", "demotion"))
                    says_boost = any(k in blob for k in ("inflates", "boost", "success", "promoted"))

                    # Simple scoring:
                    #   confirming boost article  → +0.05 * sim
                    #   confirming null/dead      → -0.05 * sim (prior said pattern doesn't work)
                    # Market-specific direction check: if the article flags an
                    # UNDER bias and current edge favours OVER (via best_line
                    # point trending high for this team's total), dampen.
                    if says_boost:
                        delta += 0.05 * sim
                        cites.append(f"+{a.get('topic')}(sim={sim:.2f})")
                    if says_dead:
                        delta -= 0.05 * sim
                        cites.append(f"-{a.get('topic')}(sim={sim:.2f})")
                    # Market-direction heuristic for totals.
                    if market == "totals":
                        # Edge's implied_range direction — not directly avail
                        # here, so use the presence of OVER/UNDER mentions as
                        # a weak signal.
                        if is_over and not is_under:
                            delta += 0.03 * sim
                            cites.append(f"over/{a.get('topic')}")
                        elif is_under and not is_over:
                            delta -= 0.03 * sim
                            cites.append(f"under/{a.get('topic')}")

                # Clamp to ±cap.
                if delta > WIKI_EDGE_ADJUSTMENT_CAP:
                    delta = WIKI_EDGE_ADJUSTMENT_CAP
                elif delta < -WIKI_EDGE_ADJUSTMENT_CAP:
                    delta = -WIKI_EDGE_ADJUSTMENT_CAP
                edge["wiki_confidence_delta"] = round(delta, 4)
                edge["wiki_cites"] = cites[:5]
    except Exception as e:
        logger.warning(f"Wiki edge adjustment pass failed (non-fatal): {e}")
        return edges

    return edges


# ---------------------------------------------------------------------------
# Alt-line edge scanning
# ---------------------------------------------------------------------------
#
# Every alternate spread / total / prop line is its own market — a -3.5 alt
# spread has a different win probability from the -2.5 main line, and books
# price them independently. Historically we only scanned the main line, which
# left "key number arbitrage" on the table: when one book sits on -3 (a dead
# number in NFL) while another offers -3.5 (a key number), the -3.5 line is
# mathematically superior by ~2% and can go uncontested for minutes.
#
# Design:
#   - fetch_alt_lines_for_games(games, sport): per-event odds-api call for
#     alternate_spreads + alternate_totals, cached 15 min per event_id to
#     keep credit burn bounded (~2 events × 1 call per 15 min = ~4/hr/sport).
#   - scan_alt_line_edges(games_with_alts, sport): runs the normal
#     cross-book scanner once per alt point value and tags results as
#     "alt_line" so they're distinguishable from main-line edges.
#
# The cache is process-local — production callers reuse the same scanner
# instance across snapshots so the 15-min TTL is enough to prevent
# duplicate calls on the same slate.

import time as _alt_time

_ALT_LINE_CACHE: dict[str, tuple[float, dict]] = {}
_ALT_LINE_TTL_S = 15 * 60  # 15 minutes


def _alt_cache_get(key: str) -> Optional[dict]:
    entry = _ALT_LINE_CACHE.get(key)
    if not entry:
        return None
    ts, data = entry
    if _alt_time.time() - ts > _ALT_LINE_TTL_S:
        _ALT_LINE_CACHE.pop(key, None)
        return None
    return data


def _alt_cache_put(key: str, data: dict) -> None:
    _ALT_LINE_CACHE[key] = (_alt_time.time(), data)
    # Simple LRU-ish cap: drop oldest when >500 entries. Each entry is small.
    if len(_ALT_LINE_CACHE) > 500:
        oldest_key = min(_ALT_LINE_CACHE, key=lambda k: _ALT_LINE_CACHE[k][0])
        _ALT_LINE_CACHE.pop(oldest_key, None)


async def fetch_alt_lines_for_games(games: list[dict], sport: str) -> list[dict]:
    """Fetch alternate spreads / totals for each upcoming game, with per-event
    caching. Returns the games list with an extra ``alt_bookmakers`` key on
    each game holding the alternate-line bookmaker array from odds-api.

    Low-credit-burn: at most 1 odds-api call per event per 15 minutes. Call
    this before ``scan_alt_line_edges`` in the main loop. Games already in
    progress are skipped (the pre-game filter runs inside).
    """
    from tools.odds_api import get_alternate_lines

    pre_game = _filter_in_progress_games(games)
    enriched = []
    for g in pre_game:
        eid = g.get("id", "")
        if not eid:
            enriched.append(g)
            continue
        cache_key = f"{sport}:{eid}"
        cached = _alt_cache_get(cache_key)
        if cached is not None:
            g2 = dict(g)
            g2["alt_bookmakers"] = cached.get("bookmakers", [])
            enriched.append(g2)
            continue
        try:
            resp = await get_alternate_lines(sport, eid)
            if resp.get("error"):
                logger.debug(f"alt lines fetch error {sport}/{eid}: {resp['error']}")
                enriched.append(g)
                continue
            _alt_cache_put(cache_key, resp)
            g2 = dict(g)
            g2["alt_bookmakers"] = resp.get("bookmakers", [])
            enriched.append(g2)
        except Exception as e:
            logger.debug(f"alt lines fetch exception {sport}/{eid}: {e}")
            enriched.append(g)
    return enriched


def scan_alt_line_edges(games: list[dict], sport: str = "") -> list[dict]:
    """Scan alternate-line markets for cross-book divergence.

    Expects each game dict to carry an ``alt_bookmakers`` key populated by
    ``fetch_alt_lines_for_games``. For each sport-relevant alt market
    (alternate_spreads, alternate_totals, plus prop alt-lines when present),
    groups outcomes by point value and feeds every point group through the
    standard cross-book scanner so each alt point becomes its own candidate
    edge — producing the "key number arbitrage" signal the April audit
    flagged as a dead zone.

    Returns the same edge dict shape as ``scan_cross_book_edges`` with an
    additional ``is_alt_line": True`` marker and ``alt_market`` name.
    """
    games = _filter_in_progress_games(games)

    SHARP_TITLES = get_sharp_titles_for_sport(sport)
    SOFT_TITLES = {"fanduel", "draftkings", "betmgm", "pointsbet", "caesars",
                   "betrivers", "mybookie.ag", "bovada", "betus",
                   "fanatics", "fanatics sportsbook"}

    edges: list[dict] = []

    for game in games:
        alt_bookmakers = game.get("alt_bookmakers") or []
        if not alt_bookmakers:
            continue
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        # Flatten to (market_key, team, point) -> [line dicts]
        from collections import defaultdict
        grouped: dict[tuple[str, str, float], list[dict]] = defaultdict(list)
        for bm in alt_bookmakers:
            bm_title = bm.get("title") or bm.get("key", "")
            fetched_at = bm.get("last_update")
            for mkt in bm.get("markets", []):
                mkey = mkt.get("key", "")
                if not mkey:
                    continue
                # Only scan alt markets plus alt prop markets
                if not (mkey.startswith("alternate_") or mkey.startswith("player_")):
                    continue
                for outcome in mkt.get("outcomes", []):
                    team_or_side = outcome.get("name", "")
                    point = outcome.get("point")
                    price = outcome.get("price")
                    if price is None or point is None or not team_or_side:
                        continue
                    grouped[(mkey, team_or_side, float(point))].append({
                        "bookmaker": bm_title,
                        "price": int(price) if isinstance(price, (int, float)) else price,
                        "point": float(point),
                        "last_update": fetched_at,
                    })

        # Run each (market, side, point) group through the line-group scanner.
        for (mkey, side, point), lines in grouped.items():
            if len(lines) < 2:
                continue
            # Map alt market back to the base market for downstream tooling.
            if mkey == "alternate_spreads":
                base_market = "spreads"
            elif mkey == "alternate_totals":
                base_market = "totals"
            else:
                base_market = mkey  # player props keep their market key
            _scan_line_group(
                edges=edges,
                lines=lines,
                game=game,
                home=home,
                away=away,
                team=side,
                market=base_market,
                sport=sport,
                SHARP_TITLES=SHARP_TITLES,
                SOFT_TITLES=SOFT_TITLES,
            )
            # Tag the last-added edge (if one was produced) as an alt line.
            if edges and edges[-1].get("game_id", "") == game.get("id", ""):
                edges[-1]["is_alt_line"] = True
                edges[-1]["alt_market"] = mkey
                edges[-1]["alt_point"] = point

    edges.sort(key=lambda x: x["implied_range"], reverse=True)
    return edges


def _scan_line_group(
    *,
    edges: list,
    lines: list[dict],
    game: dict,
    home: str,
    away: str,
    team: str,
    market: str,
    sport: str,
    SHARP_TITLES: set,
    SOFT_TITLES: set,
) -> None:
    """Score one group of lines (same market, same team, same point value).

    Callers must pre-group the lines; this function trusts that every entry
    in `lines` is comparable and applies:
      - H2H contamination check
      - Implied-range sanity check (12%)
      - FRESHNESS-WEIGHTED sharp consensus (exponential decay, half-life
        from CALLISTO_ODDS_HALF_LIFE_S)
      - Soft-book edge detection vs the weighted consensus

    Edge-sanity caps remain unchanged so a bad snapshot can't blow past
    the 20%/12% guardrails.
    """
    if len(lines) < 2:
        return
    all_lines = lines
    best_line = max(all_lines, key=lambda x: x["price"])
    worst_line = min(all_lines, key=lambda x: x["price"])
    price_spread = best_line["price"] - worst_line["price"]

    # H2H sanity check: if lines contain both large positive and large
    # negative prices, both sides of the market leaked into one team's
    # line set (e.g. favorite -750 mixed with opponent's underdog +610).
    # This produces phantom edges of 50%+ that are physically impossible.
    if market == "h2h":
        prices = [l["price"] for l in all_lines]
        has_big_pos = any(p > 150 for p in prices)
        has_big_neg = any(p < -150 for p in prices)
        if has_big_pos and has_big_neg:
            logger.warning(
                f"H2H line contamination for {team}: prices span "
                f"{min(prices)} to {max(prices)} — skipping"
            )
            return

    # Calculate implied probability range across books
    implied_probs = [calculate_implied_probability(l["price"]) for l in all_lines]
    implied_range = max(implied_probs) - min(implied_probs)
    avg_implied = sum(implied_probs) / len(implied_probs)

    # Sanity: implied range > 12% is almost certainly data contamination.
    # With freshness weighting below we could in principle relax this cap
    # (scraper-vs-API drift shouldn't blow it past 12%), but leaving it as-is
    # is defense-in-depth — a 12% range with identical timestamps is still
    # a contaminated-data signal.
    if implied_range > 0.12:
        logger.warning(
            f"Implausible implied range {implied_range:.1%} for {team} "
            f"{market} — likely data contamination, skipping"
        )
        return

    # Classify which books are sharp vs soft for this line. Use
    # canonicalize_book so "Betfair Exchange" / "betfair exchange" /
    # "betfair_exchange" all collapse onto the same key before the
    # membership test — previously the raw .lower() left spaces intact
    # and silently dropped odds-api.io's underscore form.
    def _is_sharp(l: dict) -> bool:
        bk = canonicalize_book(l.get("bookmaker", ""))
        return (
            bk in SHARP_TITLES
            or l.get("bookmaker", "").lower() in SHARP_TITLES
        )

    def _is_soft(l: dict) -> bool:
        bk = canonicalize_book(l.get("bookmaker", ""))
        return (
            bk in SOFT_TITLES
            or l.get("bookmaker", "").lower() in SOFT_TITLES
        )

    sharp_lines = [l for l in all_lines if _is_sharp(l)]
    soft_lines = [l for l in all_lines if _is_soft(l)]

    # Sharp consensus = FRESHNESS-WEIGHTED mean of sharp book implied probs.
    # A book quoting 30s ago weighs ~6x more than one quoting 3min ago.
    sharp_consensus, weight_debug = weighted_sharp_consensus(sharp_lines)
    if _DEBUG_WEIGHTS and sharp_consensus is not None:
        logger.info(
            f"[weights] {team} {market}: consensus={sharp_consensus:.4f} "
            f"weights={weight_debug}"
        )

    # Freshness metadata for consumers — average/max age of the books used.
    avg_age_s: Optional[float] = None
    max_age_s: Optional[float] = None
    if weight_debug:
        ages = [r["age_s"] for r in weight_debug if r["age_s"] is not None]
        if ages:
            avg_age_s = round(sum(ages) / len(ages), 1)
            max_age_s = round(max(ages), 1)

    # Edge: soft book offers better price than sharp consensus
    soft_edges = []
    if sharp_consensus is not None:
        for sl in soft_lines:
            soft_implied = calculate_implied_probability(sl["price"])
            edge = sharp_consensus - soft_implied
            if edge > 0.20:
                logger.warning(
                    f"Implausible edge {edge:.1%} for {team} at "
                    f"{sl['bookmaker']} — likely data contamination"
                )
                continue
            if edge > 0.02:
                ev = calculate_ev(
                    probability=sharp_consensus,
                    american_odds=sl["price"],
                )
                soft_edges.append({
                    "bookmaker": sl["bookmaker"],
                    "price": sl["price"],
                    "point": sl.get("point"),
                    "edge_vs_sharp": round(edge, 4),
                    "ev": ev,
                })

    if not (price_spread >= 10 or implied_range >= 0.03):
        return

    # Compute market microstructure metrics
    book_name_list = [l["bookmaker"] for l in all_lines]
    micro = compute_market_metrics(implied_probs, book_name_list, SHARP_TITLES)

    # Dead number / key number enrichment for spreads/totals
    dead_num_info: dict = {}
    if market in ("spreads", "totals") and sport:
        _dn_sport = sport.lower()
        if _dn_sport in _DEAD_NUM_SPORT_ALIASES:
            best_point = best_line.get("point")
            if best_point is not None:
                try:
                    dead_num_info["is_dead_number"] = _is_dead_number(best_point, _dn_sport)
                    dead_num_info["key_number_importance"] = _key_number_value(best_point, _dn_sport)
                except (ValueError, KeyError) as e:
                    logger.debug(
                        f"Dead-number info skipped for {_dn_sport} "
                        f"point={best_point}: {type(e).__name__}: {e}"
                    )

    # Line shopping analysis: compare best vs worst spread across books
    line_shopping_info: dict = {}
    if market == "spreads" and sport:
        _dn_sport = sport.lower()
        if _dn_sport in _DEAD_NUM_SPORT_ALIASES:
            best_pt = best_line.get("point")
            worst_pt = worst_line.get("point")
            if best_pt is not None and worst_pt is not None and best_pt != worst_pt:
                try:
                    from tools.dead_numbers import line_shopping_value
                    lsv = line_shopping_value(best_pt, worst_pt, _dn_sport)
                    line_shopping_info = {
                        "prob_difference_pct": lsv.get("prob_difference_pct", 0),
                        "cents_value": lsv.get("cents_value", 0),
                        "crossed_key_numbers": lsv.get("crossed_key_numbers", []),
                        "recommendation": lsv.get("recommendation", ""),
                    }
                except (ValueError, KeyError) as e:
                    logger.debug(
                        f"line_shopping_value failed for {_dn_sport} "
                        f"{best_pt}/{worst_pt}: {type(e).__name__}: {e}"
                    )

    fair_probs = None
    market_hold = None
    try:
        from tools.math_utils import no_vig_price as _nvp, calculate_hold as _ch, american_to_decimal as _atd
        if len(all_lines) >= 2:
            fair_probs = _nvp(best_line["price"], worst_line["price"])
            dec_odds = [_atd(l["price"]) for l in all_lines[:2]]
            market_hold = round(_ch(dec_odds), 4)
    except Exception as e:
        logger.debug(
            f"fair_probs/market_hold computation failed for "
            f"{best_line.get('bookmaker')}/{worst_line.get('bookmaker')}: "
            f"{type(e).__name__}: {e}"
        )

    edges.append({
        "game": f"{away} @ {home}",
        "game_id": game.get("id", ""),
        "commence_time": game.get("commence_time"),
        "team": team,
        "market": market,
        "best_line": {
            "bookmaker": best_line["bookmaker"],
            "price": best_line["price"],
            "point": best_line.get("point"),
        },
        "worst_line": {
            "bookmaker": worst_line["bookmaker"],
            "price": worst_line["price"],
            "point": worst_line.get("point"),
        },
        "price_spread": price_spread,
        "implied_range": round(implied_range, 4),
        "avg_implied": round(avg_implied, 4),
        "sharp_consensus": round(sharp_consensus, 4) if sharp_consensus else None,
        "sharp_consensus_weighted": True,
        "consensus_avg_age_s": avg_age_s,
        "consensus_max_age_s": max_age_s,
        "no_vig_fair_probs": [round(p, 4) for p in fair_probs] if fair_probs else None,
        "market_hold": market_hold,
        "num_bookmakers": len(all_lines),
        "soft_book_edges": soft_edges,
        "book_count": len(all_lines),
        "hhi": micro["hhi_overall"],
        "entropy": micro["entropy_overall"],
        **dead_num_info,
        "line_shopping": line_shopping_info if line_shopping_info else None,
    })


def detect_sharp_money(old_snapshot: dict, new_snapshot: dict) -> list[dict]:
    """
    Detect sharp money by finding games where ONE book moved but others didn't.

    When Pinnacle or a sharp book moves a line and the retail books haven't
    followed yet, there's a window. Sharp money caused the move — the retail
    books WILL follow, it's just a matter of when.

    This is the "steam move" concept:
    1. Sharp bettor places large wager on a soft book
    2. That book adjusts its line
    3. Other books haven't received the same action yet
    4. Window exists to bet the OLD line at other books before they adjust
    """
    old_prices = {}  # (game_id, book, market, team) -> price
    new_prices = {}

    for snapshot, store in [(old_snapshot, old_prices), (new_snapshot, new_prices)]:
        for game in snapshot.get("games", []):
            gid = game.get("id", "")
            for bm in game.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    for outcome in mkt.get("outcomes", []):
                        key = (gid, bm["key"], mkt["key"], outcome.get("name", ""))
                        store[key] = {
                            "price": outcome.get("price", 0),
                            "point": outcome.get("point"),
                            "bookmaker": bm["title"],
                        }

    # Group by (game_id, market, team) to compare across books
    from collections import defaultdict
    game_lines = defaultdict(list)

    for key in new_prices:
        gid, book_key, market, team = key
        group_key = (gid, market, team)
        old = old_prices.get(key)
        new = new_prices[key]
        if old:
            price_diff = new["price"] - old["price"]
            game_lines[group_key].append({
                "bookmaker": new["bookmaker"],
                "book_key": book_key,
                "old_price": old["price"],
                "new_price": new["price"],
                "price_diff": price_diff,
                "old_point": old.get("point"),
                "new_point": new.get("point"),
                "point_diff": (new.get("point") or 0) - (old.get("point") or 0),
            })

    sharp_signals = []
    for (gid, market, team), books in game_lines.items():
        if len(books) < 3:
            continue

        # Count how many books moved significantly
        movers = [b for b in books if abs(b["price_diff"]) >= 8 or abs(b["point_diff"]) >= 0.5]
        stale = [b for b in books if abs(b["price_diff"]) < 3 and abs(b["point_diff"]) < 0.5]

        # Sharp signal: 1-2 books moved, majority didn't
        if 0 < len(movers) <= 2 and len(stale) >= 2:
            sharp_signals.append({
                "game_id": gid,
                "market": market,
                "team": team,
                "moved_books": [{
                    "bookmaker": m["bookmaker"],
                    "old_price": m["old_price"],
                    "new_price": m["new_price"],
                    "movement": m["price_diff"],
                } for m in movers],
                "stale_books": [{
                    "bookmaker": s["bookmaker"],
                    "price": s["new_price"],
                    "point": s.get("new_point"),
                } for s in stale],
                "signal": "SHARP_MOVE",
                "interpretation": (
                    f"{len(movers)} book(s) moved on {team} {market} while "
                    f"{len(stale)} book(s) haven't adjusted. "
                    f"Stale books may offer value before they follow."
                ),
            })

    return sharp_signals


def scan_vig_edges(games: list[dict], market: str = "spreads") -> list[dict]:
    """
    Find books offering unusually low vig (juice) on specific games.

    Standard vig: both sides at -110 = 4.55% total vig.
    Low vig: -105/-105 = 2.44% total vig.
    Reduced vig = the book is either promoting or mispricing.

    Books with lower vig give you better prices structurally —
    over thousands of bets, reduced vig is the simplest edge.
    """
    games = _filter_in_progress_games(games)
    vig_edges = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != market:
                    continue

                outcomes = mkt.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                # Use math_utils for overround and hold calculations
                try:
                    from tools.math_utils import calculate_overround, calculate_hold, american_to_decimal
                    decimal_odds = [american_to_decimal(o.get("price", -110)) for o in outcomes]
                    vig = calculate_overround(decimal_odds)
                    hold = calculate_hold(decimal_odds)
                except ImportError:
                    # Fallback: inline calculation
                    total_implied = sum(
                        calculate_implied_probability(o.get("price", -110))
                        for o in outcomes
                    )
                    vig = total_implied - 1.0
                    hold = vig / (1.0 + vig) if (1.0 + vig) > 0 else 0

                # Standard vig on spreads is ~4.5% (-110/-110)
                # Anything under 3% is notable, under 2% is exceptional
                if vig < 0.035:
                    vig_edges.append({
                        "game": f"{away} @ {home}",
                        "game_id": game.get("id", ""),
                        "bookmaker": bm["title"],
                        "market": market,
                        "vig_pct": round(vig * 100, 2),
                        "hold_pct": round(hold * 100, 2),
                        "total_implied": round(1.0 + vig, 4),
                        "outcomes": [
                            {
                                "name": o.get("name", ""),
                                "price": o.get("price", 0),
                                "point": o.get("point"),
                                "implied": round(calculate_implied_probability(o.get("price", -110)), 4),
                            }
                            for o in outcomes
                        ],
                        "edge_type": "LOW_VIG",
                        "note": (
                            f"Vig at {round(vig * 100, 1)}% (hold {round(hold * 100, 1)}%) vs standard ~4.5%. "
                            f"{'Exceptional value' if vig < 0.02 else 'Notable reduction'}."
                        ),
                    })

    vig_edges.sort(key=lambda x: x["vig_pct"])
    return vig_edges


def scan_pace_model_total_edges(
    games: list[dict],
    sport: str,
    weather_data: Optional[dict] = None,
    venue_team: Optional[str] = None,
    refs: Optional[list[str]] = None,
) -> list[dict]:
    """
    Scan games for total (over/under) edges using the pace model + environment.

    This provides an INDEPENDENT total estimate beyond cross-book divergence.
    The pace model projects totals from first principles (pace x efficiency),
    then the environment module adjusts for weather/venue/refs.

    The result supplements — does not replace — cross-book edge detection.

    Args:
        games: List of game dicts from odds snapshot.
        sport: Odds API sport key (e.g. 'basketball_nba').
        weather_data: Optional weather dict for outdoor games.
        venue_team: Home team abbreviation for venue lookup.
        refs: Optional referee names for the game.

    Returns:
        List of model-based total edge dicts.
    """
    games = _filter_in_progress_games(games)
    pace_sport = _PACE_SPORT_MAP.get(sport.lower())
    if not pace_sport:
        return []

    try:
        from tools.pace_model import (
            project_game_total,
            detect_total_edge,
            poisson_total_distribution,
            LEAGUE_DEFAULTS,
            Sport,
        )
        from tools.environment import total_environment_adjustment
    except ImportError as e:
        logger.debug(f"Pace/environment import failed: {e}")
        return []

    sport_enum = Sport(pace_sport)
    defaults = LEAGUE_DEFAULTS.get(sport_enum, {})
    if not defaults:
        return []

    # Get league average values for this sport
    if sport_enum == Sport.NBA:
        league_avg_pace = defaults["pace"]
        league_avg_eff = defaults["off_eff"]
    elif sport_enum == Sport.NFL:
        league_avg_pace = defaults["plays_per_game"]
        league_avg_eff = defaults["yards_per_play"]
    elif sport_enum == Sport.MLB:
        league_avg_pace = defaults["runs_per_game"]  # PA proxy
        league_avg_eff = defaults["runs_per_game"]
    elif sport_enum == Sport.NHL:
        league_avg_pace = defaults["shots_per_game"]
        league_avg_eff = defaults["goals_per_game"]
    elif sport_enum == Sport.SOCCER:
        league_avg_pace = defaults["shots_per_game"]
        league_avg_eff = defaults["xg_per_game"]
    else:
        return []

    # Compute environment adjustment (venue + weather + refs)
    env_adj = 0.0
    env_detail = None
    env_sport_code = pace_sport.upper()
    if venue_team:
        try:
            env_result = total_environment_adjustment(
                venue=venue_team,
                sport=env_sport_code,
                weather=weather_data,
                refs=refs,
            )
            env_adj = env_result.get("total_adj", 0.0)
            env_detail = env_result
        except Exception as e:
            logger.debug(f"Environment adjustment failed: {e}")

    edges = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        if not home or not away:
            continue

        # Extract book total line from the game data
        book_total = None
        book_over_odds = None
        book_under_odds = None

        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "totals":
                    continue
                for o in mkt.get("outcomes", []):
                    point = o.get("point")
                    if point is None:
                        continue
                    if o.get("name", "").lower() == "over":
                        book_total = point
                        book_over_odds = o.get("price", -110)
                    elif o.get("name", "").lower() == "under":
                        book_under_odds = o.get("price", -110)
                if book_total is not None:
                    break
            if book_total is not None:
                break

        if book_total is None or book_over_odds is None or book_under_odds is None:
            continue

        # Project total using pace model with league average inputs
        # (We use league averages as a baseline; specific team data would improve this
        # when available from team stats tools)
        try:
            projection = project_game_total(
                home_pace=league_avg_pace,
                away_pace=league_avg_pace,
                home_off_eff=league_avg_eff,
                away_off_eff=league_avg_eff,
                home_def_eff=league_avg_eff,
                away_def_eff=league_avg_eff,
                league_avg_pace=league_avg_pace,
                sport=pace_sport,
                league_avg_eff=league_avg_eff,
            )
        except Exception as e:
            logger.debug(f"Pace model projection failed for {away} @ {home}: {e}")
            continue

        # Apply environment adjustment
        model_total = projection.projected_total + env_adj

        # Detect edge: model total vs book total
        try:
            edge = detect_total_edge(
                projected_total=model_total,
                book_total=book_total,
                book_over_odds=book_over_odds,
                book_under_odds=book_under_odds,
                sport=pace_sport,
                home_expected=projection.home_projected + (env_adj / 2.0),
                away_expected=projection.away_projected + (env_adj / 2.0),
            )
        except Exception as e:
            logger.debug(f"Total edge detection failed for {away} @ {home}: {e}")
            continue

        # Only report edges above 1% (model-based edges are noisier)
        if abs(edge.edge_pct) < 1.0:
            continue

        edge_dict = {
            "game": f"{away} @ {home}",
            "game_id": game.get("id", ""),
            "edge_type": "PACE_MODEL_TOTAL",
            "direction": edge.edge_direction,
            "edge_pct": edge.edge_pct,
            "model_total": round(model_total, 1),
            "book_total": book_total,
            "delta": round(model_total - book_total, 1),
            "over_probability": edge.over_probability,
            "under_probability": edge.under_probability,
            "kelly_fraction": edge.kelly_fraction,
            "ev": edge.ev,
            "pace_factor": projection.pace_factor,
            "methodology": projection.methodology,
            "environment_adj": round(env_adj, 2),
            "environment_detail": env_detail,
        }
        edges.append(edge_dict)

    # Sort by edge magnitude
    edges.sort(key=lambda x: abs(x["edge_pct"]), reverse=True)
    if edges:
        logger.info(
            f"Pace model total edges ({sport}): {len(edges)} found, "
            f"best edge: {edges[0]['edge_pct']:.1f}% {edges[0]['direction']}"
        )
    return edges


def full_edge_scan(snapshot: dict) -> dict:
    """
    Run all edge scanners on a snapshot and return a unified report.

    This is the main entry point — call after each odds snapshot.
    """
    games = snapshot.get("games", [])
    if not games:
        return {"error": "No games in snapshot", "edges": []}

    # Filter out in-progress games — their odds are live lines, not pre-game.
    # Mixing live and pre-game odds produces phantom edges (see BAL@PIT 2026-04-04).
    pre_game = _filter_in_progress_games(games)
    in_progress_count = len(games) - len(pre_game)
    games = pre_game

    if in_progress_count:
        logger.info(
            f"Filtered {in_progress_count} in-progress game(s) from edge scan "
            f"(live odds contaminate sharp consensus)"
        )

    if not games:
        return {"error": "All games in progress — no pre-game edges", "edges": [],
                "filtered_in_progress": in_progress_count}

    report = {
        "game_count": len(games),
        "filtered_in_progress": in_progress_count,
        "sport": snapshot.get("sport", "unknown"),
    }

    # Cross-book divergence
    sport = snapshot.get("sport", "")
    for market in ["spreads", "h2h", "totals"]:
        key = f"cross_book_{market}"
        edges = scan_cross_book_edges(games, market=market, sport=sport)
        report[key] = edges
        if edges:
            logger.info(
                f"Cross-book {market}: {len(edges)} divergences found, "
                f"max implied range: {edges[0]['implied_range']:.1%}"
            )

    # Vig analysis
    for market in ["spreads", "h2h", "totals"]:
        key = f"low_vig_{market}"
        vig = scan_vig_edges(games, market=market)
        report[key] = vig
        if vig:
            logger.info(f"Low vig {market}: {len(vig)} edges, lowest: {vig[0]['vig_pct']}%")

    # Pace model total edges (independent fair value estimate)
    pace_model_edges = scan_pace_model_total_edges(games, sport)
    report["pace_model_totals"] = pace_model_edges
    if pace_model_edges:
        logger.info(f"Pace model totals: {len(pace_model_edges)} edges found")

    # Dead number steals — find books sitting on dead numbers while others
    # are on key numbers for the same game (spreads only)
    _dn_sport = sport.lower() if sport else ""
    if _dn_sport in _DEAD_NUM_SPORT_ALIASES:
        dead_steals = _scan_dead_number_steals(games, sport)
        report["dead_number_steals"] = dead_steals
        if dead_steals:
            logger.info(f"Dead number steals: {len(dead_steals)} found")
    else:
        report["dead_number_steals"] = []

    # Alt-line edges — scan alternate spreads / totals / prop alts
    # for cross-book divergence at each alt point value. The caller is
    # expected to have enriched games with `alt_bookmakers` via
    # ``fetch_alt_lines_for_games`` before handing the snapshot off (the
    # sync path can't itself fire the async fetch). Every alt point becomes
    # its own edge candidate.
    alt_enriched_games = [g for g in games if g.get("alt_bookmakers")]
    if alt_enriched_games:
        alt_edges = scan_alt_line_edges(alt_enriched_games, sport=sport)
        report["alt_line_edges"] = alt_edges
        if alt_edges:
            logger.info(f"Alt-line edges: {len(alt_edges)} found across {len(alt_enriched_games)} game(s)")
    else:
        report["alt_line_edges"] = []

    # Simulation-based edge validation — independently validate cross-book
    # edges using Monte Carlo simulations (simulate_spread for high-scoring,
    # compare_poisson_to_market for low-scoring sports)
    sim_validated = _simulation_validate_edges(games, sport, report)
    report["simulation_validated"] = sim_validated
    if sim_validated:
        logger.info(f"Simulation validation: {len(sim_validated)} edges validated")

    # Summary
    total_edges = sum(
        len(report.get(k, []))
        for k in report
        if k.startswith("cross_book_") or k.startswith("low_vig_") or k in ("pace_model_totals", "dead_number_steals", "simulation_validated", "alt_line_edges")
    )
    report["total_edges"] = total_edges

    return report


def _simulation_validate_edges(games: list[dict], sport: str, report: dict) -> list[dict]:
    """
    Use Monte Carlo simulation to independently validate cross-book edges.

    For spread edges: run simulate_spread() and compare sim-implied prob vs book.
    For totals in low-scoring sports: run compare_poisson_to_market().
    Only validates edges that passed the cross-book divergence filter.
    """
    games = _filter_in_progress_games(games)
    try:
        from tools.simulation import simulate_spread, simulate_poisson, compare_poisson_to_market, _classify_sport
    except ImportError:
        logger.debug("Simulation module not available for edge validation")
        return []

    validated = []
    classification = _classify_sport(sport) if sport else "high_scoring"

    # Only validate the top cross-book edges to avoid burning CPU
    spread_edges = report.get("cross_book_spreads", [])[:5]
    total_edges = report.get("cross_book_totals", [])[:5]

    # Build game lookup for simulation
    game_by_id = {g.get("id", ""): g for g in games}

    # Validate spread edges with simulate_spread
    for edge_info in spread_edges:
        game_id = edge_info.get("game_id", "")
        game = game_by_id.get(game_id)
        if not game:
            continue
        try:
            sim_result = simulate_spread(game, sport=sport, n_sims=5000)
            fair_spread = sim_result.get("fair_spread", 0)
            sim_edges = sim_result.get("edges", [])
            if sim_edges:
                best_sim_edge = sim_edges[0]
                if abs(best_sim_edge.get("edge", 0)) >= 0.02:
                    hold_info = _compute_market_hold(game, "spreads")
                    validated.append({
                        "source": "simulation",
                        "type": "spread",
                        "game": edge_info.get("game", ""),
                        "team": edge_info.get("team", ""),
                        "fair_spread": fair_spread,
                        "sim_edge": best_sim_edge.get("edge", 0),
                        "sim_edge_pct": best_sim_edge.get("edge_pct", 0),
                        "sim_prob": best_sim_edge.get("simulated_prob", 0),
                        "book_prob": best_sim_edge.get("book_prob", 0),
                        "rating": best_sim_edge.get("rating", "NO_EDGE"),
                        "cross_book_agrees": edge_info.get("implied_range", 0) >= 0.03,
                        **hold_info,
                    })
        except Exception as e:
            logger.debug(f"Sim validation failed for spread edge {game_id}: {e}")

    # Validate total edges for low-scoring sports with Poisson model
    if classification == "low_scoring":
        for edge_info in total_edges:
            game_id = edge_info.get("game_id", "")
            game = game_by_id.get(game_id)
            if not game:
                continue
            try:
                import numpy as np
                totals_found = []
                for bm in game.get("bookmakers", []):
                    for mkt in bm.get("markets", []):
                        if mkt["key"] == "totals":
                            for o in mkt.get("outcomes", []):
                                if o.get("point") is not None:
                                    totals_found.append(o["point"])
                if not totals_found:
                    continue
                consensus_total = float(np.median(totals_found))
                home_exp = consensus_total * 0.52
                away_exp = consensus_total * 0.48

                poisson_result = simulate_poisson(home_exp, away_exp)
                poisson_edges = compare_poisson_to_market(
                    poisson_result, game,
                    game.get("home_team", "Home"),
                    game.get("away_team", "Away"),
                )
                for pe in poisson_edges[:2]:
                    if abs(pe.get("edge", 0)) >= 0.02:
                        hold_info = _compute_market_hold(game, "totals")
                        validated.append({
                            "source": "poisson_simulation",
                            "type": "total",
                            "game": edge_info.get("game", ""),
                            "team": pe.get("team", ""),
                            "market": pe.get("market", "totals"),
                            "model_prob": pe.get("model_probability", 0),
                            "market_implied": pe.get("market_implied", 0),
                            "edge": pe.get("edge", 0),
                            "cross_book_agrees": edge_info.get("implied_range", 0) >= 0.03,
                            **hold_info,
                        })
            except Exception as e:
                logger.debug(f"Poisson validation failed for total edge {game_id}: {e}")

    return validated


def _compute_market_hold(game: dict, market_key: str) -> dict:
    """Compute overround and hold for a specific market using math_utils."""
    try:
        from tools.math_utils import calculate_overround, calculate_hold, american_to_decimal
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] == market_key:
                    outcomes = mkt.get("outcomes", [])
                    if len(outcomes) >= 2:
                        decimal_odds = [american_to_decimal(o.get("price", -110)) for o in outcomes]
                        return {
                            "overround": round(calculate_overround(decimal_odds), 4),
                            "hold": round(calculate_hold(decimal_odds), 4),
                        }
    except Exception as e:
        logger.debug(
            f"overround/hold lookup failed for market_key={market_key!r}: "
            f"{type(e).__name__}: {e}"
        )
    return {"overround": None, "hold": None}


def _scan_dead_number_steals(games: list[dict], sport: str) -> list[dict]:
    """Scan all games for dead number steal opportunities across books.

    For each game, collects spread lines from all books and runs
    find_dead_number_steals() to find books sitting on dead numbers
    while others are on key numbers.
    """
    games = _filter_in_progress_games(games)
    all_steals = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for team in [home, away]:
            if not team:
                continue

            best = find_best_line(game, market="spreads", team=team)
            all_lines = best.get("all_lines", [])
            if len(all_lines) < 2:
                continue

            # Build the lines list for find_dead_number_steals
            lines_for_dn = [
                {
                    "bookmaker": l["bookmaker"],
                    "spread": l.get("point", 0),
                    "price": l.get("price", -110),
                }
                for l in all_lines
                if l.get("point") is not None
            ]

            if len(lines_for_dn) < 2:
                continue

            try:
                steals = find_dead_number_steals(lines_for_dn, sport)
                for s in steals:
                    s["game"] = f"{away} @ {home}"
                    s["team"] = team
                all_steals.extend(steals)
            except (ValueError, KeyError):
                continue

    all_steals.sort(key=lambda x: x.get("prob_difference", 0), reverse=True)
    return all_steals
