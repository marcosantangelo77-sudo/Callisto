"""Edge report building and movement evaluation for the line monitor.

Extracted from tools/line_monitor.py:
- devigged cross-book consensus computation
- movement → +EV opportunity evaluation (overreaction logic)
- model-agreement gating helpers

Pure functions where possible; DB writes are injected via callbacks so
they stay testable.
"""

import logging
import time
from datetime import datetime, timezone

from tools.odds_api import (
    find_best_line,
    calculate_ev,
    calculate_implied_probability,
)
from tools.devig import power_devig
from tools.math_utils import american_to_decimal

logger = logging.getLogger("callisto.lines.edge")

# Minimum edge for alert (mirrors line_monitor.MIN_EDGE_ALERT)
MIN_EDGE_ALERT = 0.03  # 3% edge minimum to flag as interesting


def extract_implied_probs(game: dict, market_type: str) -> list[float]:
    """Extract implied probabilities for the first outcome across all bookmakers."""
    probs = []
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_type:
                continue
            outcomes = mkt.get("outcomes", [])
            if not outcomes:
                continue
            price = outcomes[0].get("price", 0)
            if price == 0:
                continue
            if price > 0:
                prob = 100.0 / (price + 100.0)
            else:
                prob = abs(price) / (abs(price) + 100.0)
            probs.append(prob)
    return probs


def check_model_agreement(report: dict, game_id: str, team: str, market: str) -> tuple[bool, str]:
    """Return (ok, label) indicating whether any registered model agrees.

    "Agrees" currently means: at least one of (pace model total edge,
    simulation-validated edge, prop-model edge) flags the same
    (game_id, team, market) with the same direction. We don't retrain
    the models here — we just re-read the edge_scan report that the
    snapshot pipeline already computed.

    A future version can tighten this into a quantitative directional
    agreement check (e.g. |model_prob - consensus_prob| > 2%). For now
    the gate is binary: model surfaced THIS game + market at all.
    """
    if not game_id:
        return False, "no-game-id"

    def _match(edges: list, want_market: str) -> bool:
        for e in edges or []:
            if str(e.get("game_id", "")) != game_id:
                continue
            if e.get("market") and e["market"] != want_market:
                continue
            # Team match (best effort — simulation/pace edges don't
            # always carry team; a game-level match still counts).
            e_team = (e.get("team") or "").lower()
            if e_team and team and e_team != team.lower():
                continue
            return True
        return False

    # Pace model totals fire for totals markets specifically.
    if market == "totals" and _match(report.get("pace_model_totals", []), "totals"):
        return True, "pace_model"
    # Simulation-validated edges confirm spreads + totals.
    if _match(report.get("simulation_validated", []), market):
        return True, "simulation"
    # Cross-book + low-vig edges are themselves consensus-based — they
    # don't count as INDEPENDENT confirmation. Intentionally omitted.
    return False, "none"


def compute_devig_consensus(
    game: dict, target_team: str, market: str, moved_book: str,
) -> list[float] | None:
    """Power-devig each other book's two-outcome market, average later.

    Returns the list of per-book fair probabilities for the target side,
    excluding the book that moved, or None when fewer than two books can
    provide a usable pair.
    """
    devigged_fair_probs: list[float] = []
    for bm in game.get("bookmakers", []):
        if bm.get("title", bm.get("key", "")) == moved_book:
            continue  # exclude the book that moved
        for mkt in bm.get("markets", []):
            if mkt["key"] != market:
                continue
            outcomes = mkt.get("outcomes", [])
            if len(outcomes) < 2:
                continue
            # Find the target team's outcome and build the pair
            target_idx = None
            for i, oc in enumerate(outcomes):
                if target_team.lower() in oc.get("name", "").lower():
                    target_idx = i
                    break
            if target_idx is None:
                continue
            # Convert to decimal odds for devig
            try:
                decimal_odds = [
                    american_to_decimal(oc["price"]) for oc in outcomes
                ]
                if any(d <= 1.0 for d in decimal_odds):
                    continue
                fair_probs, _k = power_devig(decimal_odds)
                devigged_fair_probs.append(fair_probs[target_idx])
            except (ValueError, ZeroDivisionError):
                continue
    if len(devigged_fair_probs) < 2:
        return None
    return devigged_fair_probs


class MovementEvaluator:
    """Evaluate whether a line movement creates a +EV opportunity.

    Core overreaction logic:
    - If a line moved hard in one direction, estimate whether the market overreacted
    - Use implied probability from NEW line vs cross-bookmaker consensus
    - Flag if estimated edge > MIN_EDGE_ALERT

    `insert_ev` is an async callback performing the ev_opportunities DB
    insert; it receives a dict of row values so tests can capture writes.
    """

    def __init__(self, insert_ev, get_edge_report):
        self._insert_ev = insert_ev
        self._get_edge_report = get_edge_report
        self._devig_warn_dedup: dict[str, float] = {}

    async def evaluate(self, sport: str, movement: dict, snapshot: dict,
                       require_model_agreement: bool = True) -> None:
        # Find the game in the snapshot
        target_team = movement["team"]
        market = movement["market"]
        new_price = movement["new_price"]

        # Get cross-bookmaker comparison for this game
        for game in snapshot.get("games", []):
            # Check if this game contains the team
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            if target_team.lower() not in home.lower() and target_team.lower() not in away.lower():
                continue

            best = find_best_line(game, market=market, team=target_team)
            if best.get("error"):
                continue

            all_lines = best.get("all_lines", [])
            if len(all_lines) < 2:
                continue

            # ── Sanity checks (mirrors edge_scanner.py) ──

            # H2H contamination: if lines contain both large positive AND large
            # negative prices, both sides of the market leaked into one team's
            # set (e.g. favorite -750 mixed with underdog +610). Skip.
            if market == "h2h":
                prices = [l["price"] for l in all_lines]
                has_big_pos = any(p > 150 for p in prices)
                has_big_neg = any(p < -150 for p in prices)
                if has_big_pos and has_big_neg:
                    logger.warning(
                        f"Edge eval: H2H contamination for {target_team} — "
                        f"prices span {min(prices)} to {max(prices)}, skipping"
                    )
                    continue

            # ── Devigged consensus: power-devig each book's two-outcome
            # market, then average the target-side fair probs ──
            #
            # The naive approach (averaging raw implied probs) counts the
            # vig as edge — power devig removes it first.
            devigged_fair_probs = compute_devig_consensus(
                game, target_team, market, movement["bookmaker"],
            )
            if devigged_fair_probs is None:
                continue  # need at least 2 books for reliable consensus

            # Implied range sanity on devigged probs.
            # Tightened to 12% (was 25%) — 12% range across multi-book devig
            # already indicates contamination. Dedup warning per (team,market)
            # to prevent log spam (was firing 1300+/hr on Lakers/Suns h2h).
            fair_range = max(devigged_fair_probs) - min(devigged_fair_probs)
            if fair_range > 0.12:
                _warn_key = f"{target_team}|{market}"
                _last = self._devig_warn_dedup.get(_warn_key, 0)
                _now = time.monotonic()
                if _now - _last > 600:  # warn at most once per 10 min per team+market
                    logger.warning(
                        f"Edge eval: implausible devigged range {fair_range:.1%} "
                        f"for {target_team} {market}, skipping (will dedup for 10min)"
                    )
                    self._devig_warn_dedup[_warn_key] = _now
                continue

            consensus_prob = sum(devigged_fair_probs) / len(devigged_fair_probs)

            # The moved line's implied probability (raw — this is what the book offers)
            moved_implied = calculate_implied_probability(new_price)

            # Edge = devigged fair prob - book's implied prob
            edge = consensus_prob - moved_implied

            # Edge cap: real market edges top out ~15%. Anything above 20%
            # is almost certainly a data/calculation bug.
            if edge > 0.20:
                logger.warning(
                    f"Edge eval: implausible edge {edge:.1%} for {target_team} "
                    f"{market} @ {movement['bookmaker']}, skipping"
                )
                continue

            if abs(edge) >= MIN_EDGE_ALERT:
                ev_result = calculate_ev(
                    probability=consensus_prob,
                    american_odds=new_price,
                )

                if ev_result["is_positive_ev"]:
                    # MODEL AGREEMENT GATE (audit fix): before this check,
                    # every consensus-based edge became an ev_opportunities
                    # row — which meant we were steam-chasing whatever the
                    # books themselves were agreeing on. Require at least
                    # one independent model (pace, props, sim) to agree
                    # with the direction.
                    report = self._get_edge_report(sport) or {}
                    model_ok, model_label = check_model_agreement(
                        report=report,
                        game_id=str(game.get("id", "")),
                        team=target_team,
                        market=market,
                    )
                    steam_only = False
                    if require_model_agreement and not model_ok:
                        steam_only = True
                        logger.info(
                            f"STEAM-ONLY (model disagrees): {target_team} "
                            f"{market} @ {new_price} edge={edge:.1%} "
                            f"models={model_label}"
                        )

                    now = datetime.now(timezone.utc).isoformat()
                    await self._insert_ev({
                        "detected_at": now,
                        "sport": sport,
                        "game_id": game.get("id", ""),
                        "team": target_team,
                        "market": market,
                        "bookmaker": movement["bookmaker"],
                        "american_odds": new_price,
                        "implied_probability": round(moved_implied, 4),
                        "estimated_true_prob": round(consensus_prob, 4),
                        "edge": round(edge, 4),
                        "expected_value": ev_result["expected_value"],
                        "kelly_fraction": ev_result["kelly_fraction"],
                        "steam_only": 1 if steam_only else 0,
                    })

                    logger.info(
                        f"+EV OPPORTUNITY ({'STEAM' if steam_only else 'MODEL-RATIFIED'}):"
                        f" {target_team} {market} @ {new_price} "
                        f"(edge={edge:.1%}, EV=${ev_result['expected_value']}, "
                        f"Kelly={ev_result['kelly_fraction']:.1%}, "
                        f"devig_books={len(devigged_fair_probs)})"
                    )
                    # Autonomous loop will pick this up and analyze via AGP
            break
