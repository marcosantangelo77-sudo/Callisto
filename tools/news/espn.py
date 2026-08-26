"""ESPN fetchers (primary source): injuries, scoreboard lineups, coaching notes."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from tools.ingestion_tracking import tracked_ingestion
from tools.news._http import facade_get_client as _get_client
from tools.news.inference import infer_body_part, infer_severity
from tools.news.models import CoachingEvent, InjuryEvent, LineupEvent

logger = logging.getLogger("callisto.news_ingestion")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

_ESPN_SPORT_MAP = {
    "basketball_nba":        ("basketball", "nba"),
    "basketball_ncaab":      ("basketball", "mens-college-basketball"),
    "basketball_ncaaw":      ("basketball", "womens-college-basketball"),
    "americanfootball_nfl":  ("football", "nfl"),
    "americanfootball_ncaaf": ("football", "college-football"),
    "baseball_mlb":          ("baseball", "mlb"),
    "icehockey_nhl":         ("hockey", "nhl"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@tracked_ingestion(
    source=lambda sport, **_: f"espn.injuries.{sport}",
    sla_seconds=1800,
)
async def fetch_espn_injuries(sport: str) -> list[InjuryEvent]:
    """Pull ESPN's injuries endpoint and parse into InjuryEvent list.

    Returns an empty list on failure (caller treats ``[]`` as partial success;
    ``tracked_ingestion`` tags status='partial' when rows==0 which is the
    correct signal for "endpoint reachable, no rows today").
    """
    m = _ESPN_SPORT_MAP.get(sport)
    if not m:
        logger.debug(f"ESPN injuries: sport {sport} not mapped")
        return []

    sport_path, league_path = m
    url = f"{ESPN_BASE}/{sport_path}/{league_path}/injuries"
    try:
        resp = await _get_client().get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN injuries {sport} fetch error: {e}")
        return []

    now = now_iso()
    out: list[InjuryEvent] = []
    for team_block in data.get("items", []) or []:
        team_name = ((team_block.get("team") or {}).get("displayName") or "").strip()
        for entry in team_block.get("injuries", []) or []:
            athlete = entry.get("athlete") or {}
            player = (athlete.get("displayName") or "").strip()
            if not player:
                continue
            status_raw = (entry.get("status") or "").strip()
            details = entry.get("details") or {}
            detail_text = " ".join(
                str(v) for v in (
                    details.get("detail"),
                    details.get("side"),
                    details.get("type"),
                    (entry.get("type") or {}).get("description") if isinstance(entry.get("type"), dict) else None,
                ) if v
            )
            status, severity = infer_severity(status_raw, detail_text)
            body_part = infer_body_part(detail_text)
            out.append(InjuryEvent(
                sport=sport,
                player_name=player,
                team=team_name or None,
                body_part=body_part,
                status=status,
                severity=severity,
                first_seen_at=now,
                source="espn.injuries",
                source_url=url,
                raw=entry,
            ))
    logger.info(f"ESPN injuries {sport}: parsed {len(out)} events")
    return out


@tracked_ingestion(
    source=lambda sport, date=None, **_: f"espn.lineups.{sport}",
    sla_seconds=900,
)
async def fetch_espn_scoreboard_lineups(sport: str, date: Optional[str] = None) -> list[LineupEvent]:
    """Pull late lineup changes from ESPN's scoreboard. Surprise starts and
    late scratches surface here before they appear on the injuries endpoint
    (which is roster-level, slower to update)."""
    m = _ESPN_SPORT_MAP.get(sport)
    if not m:
        return []
    sport_path, league_path = m
    url = f"{ESPN_BASE}/{sport_path}/{league_path}/scoreboard"
    params = {"dates": date.replace("-", "")} if date else None
    try:
        resp = await _get_client().get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN scoreboard {sport} fetch error: {e}")
        return []

    now = now_iso()
    out: list[LineupEvent] = []
    for event in data.get("events", []) or []:
        for comp in event.get("competitions", []) or []:
            for competitor in comp.get("competitors", []) or []:
                team_name = ((competitor.get("team") or {}).get("displayName") or "").strip()
                for inj in competitor.get("injuries", []) or []:
                    ath = inj.get("athlete") or {}
                    player = (ath.get("displayName") or "").strip()
                    status = (inj.get("status") or "").lower()
                    if not player:
                        continue
                    # A scoreboard-time "out" effectively is a late scratch if
                    # it wasn't on the pre-game injuries endpoint. We tag it
                    # as such; the dedupe layer against the injuries feed in
                    # news_events dedup will reconcile.
                    if status in ("out", "inactive"):
                        out.append(LineupEvent(
                            sport=sport,
                            player_name=player,
                            team=team_name or None,
                            change_type="late_scratch",
                            first_seen_at=now,
                            source="espn.scoreboard",
                            source_url=url,
                            raw=inj,
                        ))
    logger.info(f"ESPN scoreboard {sport}: {len(out)} late-scratch candidates")
    return out


@tracked_ingestion(
    source=lambda sport, date=None, **_: f"espn.coaching.{sport}",
    sla_seconds=3600,
)
async def fetch_espn_coaching(sport: str, date: Optional[str] = None) -> list[CoachingEvent]:
    """Stub — ESPN doesn't publish explicit "coaching decision" items. We
    synthesise them from scoreboard notes when the `notes` array mentions
    rest days / load management. Conservative by design; coaching news is
    high-impact and should not false-positive.
    """
    m = _ESPN_SPORT_MAP.get(sport)
    if not m:
        return []
    sport_path, league_path = m
    url = f"{ESPN_BASE}/{sport_path}/{league_path}/scoreboard"
    params = {"dates": date.replace("-", "")} if date else None
    try:
        resp = await _get_client().get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN coaching {sport} fetch error: {e}")
        return []

    now = now_iso()
    out: list[CoachingEvent] = []
    for event in data.get("events", []) or []:
        for comp in event.get("competitions", []) or []:
            notes = comp.get("notes", []) or []
            for n in notes:
                headline = (n.get("headline") or "").lower()
                if any(k in headline for k in ("rest", "load management", "dnp - coach", "dnp-coach")):
                    for competitor in comp.get("competitors", []) or []:
                        team_name = ((competitor.get("team") or {}).get("displayName") or "").strip()
                        out.append(CoachingEvent(
                            sport=sport,
                            team=team_name or "unknown",
                            decision="rest_starters",
                            affected_players=[],
                            first_seen_at=now,
                            source="espn.scoreboard.notes",
                            source_url=url,
                            raw={"headline": headline},
                        ))
    return out
