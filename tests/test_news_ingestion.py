"""Tests for tools.news_ingestion.

Covers:
  * ESPN injuries response shape → parsed InjuryEvent list
  * Cross-source dedup: same injury via 2 sources → 1 row, confirmed_at set
  * Severity inference across known keyword patterns
  * Body-part inference tokenises correctly
  * persist_news_rows is idempotent within a 6h window

Mocks out httpx so tests don't hit the network.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
import pytest


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    monkeypatch.setenv("CALLISTO_DB_PATH", path)
    # Force re-read of module-level DB_PATH
    for mod in ("tools.news_ingestion", "tools.ingestion_tracking"):
        if mod in sys.modules:
            del sys.modules[mod]
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


async def _init_ingestion_runs(db_path: str) -> None:
    """Create the tracking table so @tracked_ingestion writes don't error."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ingestion_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                status TEXT NOT NULL,
                rows_ingested INTEGER DEFAULT 0,
                error_class TEXT,
                error_message TEXT,
                duration_ms INTEGER,
                extra_json TEXT
            )
            """
        )
        await db.commit()


# ── Sample ESPN injuries response ────────────────────────────────────────

SAMPLE_ESPN_INJURIES = {
    "items": [
        {
            "team": {"displayName": "Boston Celtics", "abbreviation": "BOS"},
            "injuries": [
                {
                    "status": "Out",
                    "athlete": {
                        "displayName": "Jayson Tatum",
                        "position": {"abbreviation": "SF"},
                    },
                    "type": {"description": "Injury"},
                    "details": {
                        "detail": "sprained ankle",
                        "side": "right",
                    },
                },
                {
                    "status": "Questionable",
                    "athlete": {"displayName": "Jaylen Brown",
                                "position": {"abbreviation": "SG"}},
                    "type": {"description": "Injury"},
                    "details": {
                        "detail": "sore knee",
                    },
                },
            ],
        },
        {
            "team": {"displayName": "Los Angeles Lakers", "abbreviation": "LAL"},
            "injuries": [
                {
                    "status": "Out",
                    "athlete": {"displayName": "LeBron James",
                                "position": {"abbreviation": "SF"}},
                    "type": {"description": "Illness"},
                    "details": {"detail": "flu"},
                },
            ],
        },
    ]
}


class _FakeResponse:
    def __init__(self, json_data: Any, status_code: int = 200, text: str = ""):
        self._json = json_data
        self.status_code = status_code
        self.text = text or json.dumps(json_data)

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── Severity / body-part inference ──────────────────────────────────────

@pytest.mark.asyncio
async def test_severity_inference_covers_key_phrases(tmp_db):
    await _init_ingestion_runs(tmp_db)
    from tools.news_ingestion import infer_severity

    cases = [
        ("Out", "torn acl",                       ("out", "severe")),
        ("Out", "out for the season",             ("out", "out_indefinite")),
        ("Doubtful", "knee soreness",             ("doubtful", "moderate")),
        ("Questionable", "game-time decision",    ("questionable", "minor")),
        ("",   "placed on IR",                    ("out", "out_indefinite")),
        ("",   "ruled out for tonight",           ("out", "severe")),
        ("Probable", "minor tweak",               ("probable", "minor")),
        (None, None,                              (None, None)),
    ]
    for status_text, detail, expected in cases:
        got = infer_severity(status_text, detail)
        assert got == expected, (
            f"inputs=({status_text!r},{detail!r}) → got {got}, expected {expected}"
        )


@pytest.mark.asyncio
async def test_body_part_inference(tmp_db):
    await _init_ingestion_runs(tmp_db)
    from tools.news_ingestion import infer_body_part

    assert infer_body_part("sprained right ankle") == "lower_body"
    assert infer_body_part("shoulder surgery") == "upper_body"
    assert infer_body_part("concussion protocol") == "head"
    assert infer_body_part("lower-back spasm") == "core"
    assert infer_body_part("flu-like symptoms") == "illness"
    assert infer_body_part("no matching token") is None
    assert infer_body_part(None) is None
    # Word boundary: "background" must NOT match "back"
    assert infer_body_part("background noise") is None


# ── ESPN scraper with mocked httpx ──────────────────────────────────────

@pytest.mark.asyncio
async def test_espn_injuries_parses_events(tmp_db, monkeypatch):
    await _init_ingestion_runs(tmp_db)
    import tools.news_ingestion as ni

    class _FakeClient:
        is_closed = False
        async def get(self, url, params=None):
            return _FakeResponse(SAMPLE_ESPN_INJURIES)
        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(ni, "_get_client", lambda: _FakeClient())
    events = await ni._fetch_espn_injuries("basketball_nba")
    assert len(events) == 3
    by_player = {e.player_name: e for e in events}
    assert "Jayson Tatum" in by_player
    t = by_player["Jayson Tatum"]
    assert t.status == "out"
    assert t.severity == "severe"
    assert t.body_part == "lower_body"     # "ankle"
    assert t.team == "Boston Celtics"

    brown = by_player["Jaylen Brown"]
    assert brown.status == "questionable"
    assert brown.body_part == "lower_body"  # "knee"

    lb = by_player["LeBron James"]
    assert lb.severity == "severe"           # "Out" status
    assert lb.body_part == "illness"


# ── Cross-source dedup ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_source_dedup_sets_confirmed_at(tmp_db):
    await _init_ingestion_runs(tmp_db)
    from tools.news_ingestion import InjuryEvent, dedupe_injuries

    t1 = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc).isoformat()
    t2 = datetime(2026, 4, 22, 10, 8, 0, tzinfo=timezone.utc).isoformat()

    # Two sources report Tatum ankle injury — should collapse to 1 row.
    events = [
        InjuryEvent(
            sport="basketball_nba",
            player_name="Jayson Tatum",
            team="Boston Celtics",
            body_part="lower_body",
            status="out",
            severity="severe",
            first_seen_at=t1,
            source="espn.injuries",
            source_url="https://espn.example/inj",
            raw={"detail": "ankle"},
        ),
        InjuryEvent(
            sport="basketball_nba",
            player_name="J. Tatum",          # alias form
            team=None,
            body_part="lower_body",
            status="out",
            severity="out_indefinite",        # second source says worse
            first_seen_at=t2,
            source="rotowire.news",
            source_url="https://rotowire.example/news",
            raw={"body": "out indefinitely - ankle"},
        ),
    ]

    rows = dedupe_injuries(events)
    assert len(rows) == 1
    row = rows[0]
    assert row["player_name"] in ("Jayson Tatum", "J. Tatum")
    # Confirmed_at should be the 2nd source's first_seen_at
    assert row["confirmed_at"] == t2
    # Merged source string contains both
    assert "espn.injuries" in row["source"]
    assert "rotowire.news" in row["source"]
    # Most-severe classification retained
    assert row["severity"] == "out_indefinite"


@pytest.mark.asyncio
async def test_single_source_stays_unconfirmed(tmp_db):
    await _init_ingestion_runs(tmp_db)
    from tools.news_ingestion import InjuryEvent, dedupe_injuries

    events = [
        InjuryEvent(
            sport="basketball_nba",
            player_name="Joel Embiid",
            team="Philadelphia 76ers",
            body_part="lower_body",
            status="questionable",
            severity="minor",
            first_seen_at="2026-04-22T10:00:00+00:00",
            source="espn.injuries",
            source_url=None,
            raw={},
        ),
    ]
    rows = dedupe_injuries(events)
    assert len(rows) == 1
    assert rows[0]["confirmed_at"] is None
    assert rows[0]["source"] == "espn.injuries"


@pytest.mark.asyncio
async def test_different_body_parts_do_not_collapse(tmp_db):
    """Two injuries to the same player but different body parts = 2 rows."""
    await _init_ingestion_runs(tmp_db)
    from tools.news_ingestion import InjuryEvent, dedupe_injuries

    events = [
        InjuryEvent(
            sport="basketball_nba", player_name="Kevin Durant",
            team=None, body_part="lower_body", status="out", severity="severe",
            first_seen_at="2026-04-22T10:00:00+00:00",
            source="espn.injuries", source_url=None, raw={},
        ),
        InjuryEvent(
            sport="basketball_nba", player_name="Kevin Durant",
            team=None, body_part="upper_body", status="questionable",
            severity="moderate",
            first_seen_at="2026-04-22T10:05:00+00:00",
            source="espn.injuries", source_url=None, raw={},
        ),
    ]
    rows = dedupe_injuries(events)
    assert len(rows) == 2


# ── Persistence idempotency ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_persist_news_rows_dedups_within_6h(tmp_db):
    await _init_ingestion_runs(tmp_db)
    from tools.news_ingestion import persist_news_rows

    row = {
        "sport": "basketball_nba",
        "event_id": None,
        "player_name": "Jayson Tatum",
        "event_type": "injury",
        "severity": "severe",
        "body_part": "lower_body",
        "status": "out",
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
        "confirmed_at": None,
        "source": "espn.injuries",
        "source_url": None,
        "raw_json": json.dumps({"d": 1}),
        "local_game_date": "2026-04-22",
    }
    n1 = await persist_news_rows([row], db_path=tmp_db)
    n2 = await persist_news_rows([row], db_path=tmp_db)
    assert n1 == 1
    assert n2 == 0  # dedup'd within 6h window

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute("SELECT COUNT(*) FROM news_events")
        (cnt,) = await cur.fetchone()
    assert cnt == 1


# ── Integration smoke — fetch_injuries multi-source flow ────────────────

@pytest.mark.asyncio
async def test_fetch_injuries_multi_source_with_mocks(tmp_db, monkeypatch):
    await _init_ingestion_runs(tmp_db)
    import tools.news_ingestion as ni

    class _FakeClient:
        is_closed = False
        async def get(self, url, params=None):
            if "espn" in url:
                return _FakeResponse(SAMPLE_ESPN_INJURIES)
            if "rotowire" in url:
                # Minimal HTML with one matching item — different player so
                # it doesn't collapse, confirms the secondary path fires.
                html = (
                    '<div class="news-update">'
                    '  <div class="news-update__headline">Some headline</div>'
                    '  <a data-player-id="123">Stephen Curry</a>'
                    '  <div class="news-update__news">Curry day-to-day with ankle sprain</div>'
                    '</div></div>'
                )
                return _FakeResponse({}, status_code=200, text=html)
            return _FakeResponse({})
        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(ni, "_get_client", lambda: _FakeClient())
    # Short-circuit RotoWire rate limiter so test is fast
    monkeypatch.setattr(ni._RotoWireLimiter, "wait", staticmethod(lambda *a, **k: asyncio.sleep(0)))

    rows = await ni.fetch_injuries("basketball_nba")
    players = {r["player_name"] for r in rows}
    # ESPN: Tatum, Brown, LeBron; RotoWire: Curry
    assert "Jayson Tatum" in players
    assert "Stephen Curry" in players
    # All Curry rows must have 'lower_body' body part ('ankle' keyword).
    curry = [r for r in rows if r["player_name"] == "Stephen Curry"][0]
    assert curry["body_part"] == "lower_body"
    assert curry["status"] == "questionable"  # 'day-to-day'
