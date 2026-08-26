"""Data shapes for the news ingestion package."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class InjuryEvent:
    """One injury observation. Source-specific; dedupe merges across sources."""
    sport: str
    player_name: str
    team: Optional[str]
    body_part: Optional[str]
    status: Optional[str]           # 'questionable' | 'probable' | 'doubtful' | 'out' | 'inactive'
    severity: Optional[str]          # 'minor' | 'moderate' | 'severe' | 'out_indefinite'
    first_seen_at: str               # ISO timestamp
    source: str
    source_url: Optional[str]
    raw: dict                        # per-source payload for forensic replay
    local_game_date: Optional[str] = None

    def as_news_row(self) -> dict:
        return {
            "sport": self.sport,
            "event_id": None,
            "player_name": self.player_name,
            "event_type": "injury",
            "severity": self.severity,
            "body_part": self.body_part,
            "status": self.status,
            "first_seen_at": self.first_seen_at,
            "confirmed_at": None,
            "source": self.source,
            "source_url": self.source_url,
            "raw_json": json.dumps(self.raw, default=str),
            "local_game_date": self.local_game_date,
        }


@dataclass
class LineupEvent:
    sport: str
    player_name: str
    team: Optional[str]
    change_type: str                 # 'late_scratch' | 'surprise_start' | 'position_change'
    first_seen_at: str
    source: str
    source_url: Optional[str]
    raw: dict
    local_game_date: Optional[str] = None

    def as_news_row(self) -> dict:
        return {
            "sport": self.sport,
            "event_id": None,
            "player_name": self.player_name,
            "event_type": "lineup_change",
            "severity": "moderate" if self.change_type == "late_scratch" else "minor",
            "body_part": None,
            "status": "inactive" if self.change_type == "late_scratch" else None,
            "first_seen_at": self.first_seen_at,
            "confirmed_at": None,
            "source": self.source,
            "source_url": self.source_url,
            "raw_json": json.dumps({"change_type": self.change_type, **self.raw}, default=str),
            "local_game_date": self.local_game_date,
        }


@dataclass
class CoachingEvent:
    sport: str
    team: str
    decision: str                    # 'rest_starters' | 'mop_up_lineup' | 'tactical_change'
    affected_players: list[str]      # may be empty
    first_seen_at: str
    source: str
    source_url: Optional[str]
    raw: dict
    local_game_date: Optional[str] = None

    def as_news_row(self) -> dict:
        # Coaching decisions are emitted as one row per affected player so the
        # dedup/correlation layer can key off player_name uniformly. If there
        # are no specific players named, we emit a single team-level row with
        # player_name=None.
        return {
            "sport": self.sport,
            "event_id": None,
            "player_name": None,  # overridden when iterating affected_players
            "event_type": "coaching_decision",
            "severity": "severe" if self.decision == "rest_starters" else "moderate",
            "body_part": None,
            "status": "inactive" if self.decision == "rest_starters" else None,
            "first_seen_at": self.first_seen_at,
            "confirmed_at": None,
            "source": self.source,
            "source_url": self.source_url,
            "raw_json": json.dumps(
                {"team": self.team, "decision": self.decision, **self.raw},
                default=str,
            ),
            "local_game_date": self.local_game_date,
        }
