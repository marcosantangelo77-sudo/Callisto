"""Persistence helpers for odds-api.io snapshots.

Split out of tools/odds_api_io.py — see tools/odds_io package docstring.

Currently this holds the pre-commence snapshot assembly (lookahead-free
backtesting input) and the cross-book best-line comparison, both of which
persist normalized snapshots for downstream consumers.
"""

import logging
import os
from datetime import datetime
from typing import Optional

from tools.odds_io.config import BOOKMAKER_SLUG_MAP, SELECTED_BOOKMAKERS
from tools.odds_io.normalize import (
    extract_movement_snapshots,
    normalize_event_odds,
    parse_iso,
    pick_pre_commence_entry,
    snapshot_to_market_outcomes,
)

logger = logging.getLogger("callisto.odds_api_io")


# ---------------------------------------------------------------------------
# Pre-commence snapshot — lookahead-free backtesting
# ---------------------------------------------------------------------------
#
# Closing-odds lookahead bug: prior backtests used /historical/odds which
# returns the post-settlement closing price, but stamped it as the bet-time
# snapshot. That is classic forward-looking contamination — the closing price
# already reflects every sharp move that happened AFTER a realistic bet would
# have been placed. get_historical_snapshot() replaces get_historical_odds()
# with a timestamped snapshot from /odds/movements, filtered to
# (commence_time - lead_minutes).
#
# Returns the same shape as normalize_event_odds() would, plus:
#   - snapshot_quality: 'pre_commence' | 'closing_fallback'
#   - snapshot_time:    the ACTUAL timestamp we pulled (not 00:00:00Z)
#   - lead_minutes:     what we targeted (env-overridable; default 60)


async def build_historical_snapshot(
    event_id: str | int,
    commence_time: str = "",
    minutes_before_commence: int = 60,
    bookmakers: str = "",
    markets: tuple = ("ML", "Spread", "Totals"),
    *,
    movements_fetch,
    closing_fetch,
) -> dict:
    """Assemble a timestamped PRE-COMMENCE odds snapshot for an event.

    ``movements_fetch(event_id=..., bookmaker=..., market=...)`` and
    ``closing_fetch(event_id, bookmakers=...)`` are injected I/O callables
    (in the facade these are tools.odds_api_io.get_odds_movements /
    get_historical_odds) so this pure-assembly logic stays testable without
    network access.

    Instead of returning the closing price (lookahead), this walks
    /v3/odds/movements per (book, market) and picks the LATEST snapshot that
    occurred before `commence_time - minutes_before_commence`. Guarantees that
    the returned price was actually visible at the bet-time a realistic
    strategy would have placed.

    Dual-mode fallback:
      - If no pre-commence snapshot exists for a given (book, market)
        combination (odds-api gap, or the book opened late), fall back to
        the closing odds for that book+market and TAG the book+market
        with snapshot_quality='closing_fallback'. All other (book, market)
        combos that DID have pre-commence data stay tagged
        'pre_commence'. Callers can compute a per-event mix.
      - If minutes_before_commence == 0, we skip the movements call entirely
        and return closing odds tagged 'closing_mode' (kept for comparison
        runs / regression harness).
    """
    override = os.getenv("CALLISTO_BACKTEST_LEAD_MINUTES")
    if override is not None:
        try:
            minutes_before_commence = int(override)
        except (ValueError, TypeError):
            pass

    bm_list = [b.strip() for b in (bookmakers or SELECTED_BOOKMAKERS).split(",") if b.strip()]
    commence_dt = parse_iso(commence_time) if commence_time else None

    # Closing-mode shortcut (for comparison backtests / regression harness):
    # skip the per-book movements fan-out entirely.
    if minutes_before_commence == 0 or commence_dt is None:
        closing_raw = await closing_fetch(event_id, bookmakers=",".join(bm_list))
        if isinstance(closing_raw, dict) and closing_raw.get("bookmakers"):
            normalized = normalize_event_odds(
                closing_raw,
                {"id": str(event_id), "commence_time": commence_time},
                closing_raw.get("sport_key", ""),
            )
            if normalized:
                for bm in normalized.get("bookmakers", []):
                    bm["snapshot_quality"] = "closing_mode"
                normalized["snapshot_quality_mix"] = {
                    "pre_commence": 0,
                    "closing_fallback": 0,
                    "closing_mode": len(normalized.get("bookmakers", [])),
                }
                normalized["lead_minutes"] = 0
                normalized["snapshot_time"] = commence_time or ""
                return normalized
        return {"error": "closing-mode fallback returned empty", "id": str(event_id)}

    # Fan out movements per (book, market). Each call is 1 odds-api credit.
    # We batch the markets the backtest actually consumes (ML/Spread/Totals).
    per_book_markets: dict[str, list[dict]] = {}
    per_book_qualities: dict[str, str] = {}
    home_guess = ""
    away_guess = ""
    sport_key_guess = ""
    latest_snapshot_time: Optional[datetime] = None
    mix_counts = {"pre_commence": 0, "closing_fallback": 0}

    # Fallback handle — only fetched once, lazily, if any book/market gap.
    closing_fallback_cache: Optional[dict] = None

    async def _get_closing_fallback() -> dict:
        nonlocal closing_fallback_cache
        if closing_fallback_cache is None:
            raw = await closing_fetch(event_id, bookmakers=",".join(bm_list))
            closing_fallback_cache = raw if isinstance(raw, dict) else {}
        return closing_fallback_cache

    for bm_name in bm_list:
        used_pre_commence = False
        for market in markets:
            try:
                mv = await movements_fetch(
                    event_id=event_id, bookmaker=bm_name, market=market,
                )
            except Exception as e:
                logger.debug(f"movements fetch failed {bm_name}/{market}: {e}")
                continue

            if isinstance(mv, dict) and mv.get("error"):
                # API error — skip this (book, market) and let fallback fire.
                continue

            entries = extract_movement_snapshots(mv)
            if not entries:
                continue

            pick = pick_pre_commence_entry(
                entries, commence_dt, minutes_before_commence,
            )
            if pick is None:
                # Fallback path — no pre-commence data for this slot.
                continue

            # Infer home/away if the movements payload carries it.
            for k in ("home", "homeTeam", "home_team"):
                val = mv.get(k) if isinstance(mv, dict) else None
                if val:
                    home_guess = val
                    break
            for k in ("away", "awayTeam", "away_team"):
                val = mv.get(k) if isinstance(mv, dict) else None
                if val:
                    away_guess = val
                    break
            sk = mv.get("sport_key") if isinstance(mv, dict) else None
            if sk:
                sport_key_guess = sk

            market_obj = snapshot_to_market_outcomes(
                pick["raw"], market, home_guess, away_guess,
            )
            if market_obj is None:
                continue
            market_obj["last_update"] = pick["time"].isoformat()

            per_book_markets.setdefault(bm_name, []).append(market_obj)
            used_pre_commence = True
            if latest_snapshot_time is None or pick["time"] > latest_snapshot_time:
                latest_snapshot_time = pick["time"]

        if used_pre_commence:
            per_book_qualities[bm_name] = "pre_commence"
            mix_counts["pre_commence"] += 1
        else:
            # Fallback: pull closing odds for this book.
            fb = await _get_closing_fallback()
            bm_block = (fb.get("bookmakers") or {}).get(bm_name) if isinstance(fb, dict) else None
            if bm_block:
                # bm_block is the raw market list; reuse normalize path by
                # wrapping as a single-book response.
                partial = {
                    "id": event_id,
                    "home": fb.get("home", home_guess),
                    "away": fb.get("away", away_guess),
                    "date": commence_time,
                    "bookmakers": {bm_name: bm_block},
                }
                norm = normalize_event_odds(partial, {"id": str(event_id)}, sport_key_guess or "")
                if norm and norm.get("bookmakers"):
                    per_book_markets[bm_name] = norm["bookmakers"][0].get("markets", [])
                    per_book_qualities[bm_name] = "closing_fallback"
                    mix_counts["closing_fallback"] += 1
                    logger.warning(
                        f"snapshot_quality=closing_fallback for event={event_id} book={bm_name}: "
                        f"no pre-commence movement data (lead_minutes={minutes_before_commence})"
                    )

    if not per_book_markets:
        return {
            "error": "no snapshots available (pre-commence or fallback)",
            "id": str(event_id),
            "lead_minutes": minutes_before_commence,
        }

    result = {
        "id": str(event_id),
        "home_team": home_guess,
        "away_team": away_guess,
        "commence_time": commence_time,
        "sport_key": sport_key_guess,
        "bookmakers": [],
        "snapshot_quality_mix": mix_counts,
        "lead_minutes": minutes_before_commence,
        "snapshot_time": latest_snapshot_time.isoformat() if latest_snapshot_time else commence_time,
    }
    for bm_name, markets_list in per_book_markets.items():
        bm_slug = BOOKMAKER_SLUG_MAP.get(bm_name, bm_name.lower().replace(" ", "_"))
        result["bookmakers"].append({
            "key": bm_slug,
            "title": bm_name,
            "last_update": latest_snapshot_time.isoformat() if latest_snapshot_time else "",
            "snapshot_quality": per_book_qualities.get(bm_name, "pre_commence"),
            "markets": markets_list,
        })

    return result
