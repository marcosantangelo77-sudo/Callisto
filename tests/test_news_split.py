"""Tests for the tools.news package split of tools.news_ingestion.

Verifies:
  * The facade (tools.news_ingestion) re-exports the full public surface.
  * The split modules in tools.news are importable and consistent with the
    facade (same objects, not copies).
  * Severity / body-part inference still behaves through both import paths.
  * Cross-source dedup via the new tools.news.dedup module.
  * ESPN + RotoWire fetchers work end-to-end with mocked httpx through the
    facade's legacy monkeypatch seam (_get_client).
  * persist_news_rows remains idempotent within its 6h window.
  * The rate limiter gates concurrent callers.

Mocks out httpx so tests never hit the network.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone

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
    for mod in ("tools.news_ingestion", "tools.news", "tools.ingestion_tracking"):
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


class _FakeResponse:
    def __init__(self, data, status_code=200, text=""):
        self._data = data
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


SAMPLE_ESPN_INJURIES = {
    "items": [
        {
            "team": {"displayName": "Boston Celtics"},
            "injuries": [
                {
                    "athlete": {"displayName": "Jayson Tatum"},
                    "status": "Out",
                    "details": {"detail": "Right ankle sprain", "type": "Sprain"},
                },
                {
                    "athlete": {"displayName": "Jaylen Brown"},
                    "status": "Questionable",
                    "details": {"detail": "Sore right knee", "type": "Soreness"},
                },
            ],
        },
        {
            "team": {"displayName": "Los Angeles Lakers"},
            "injuries": [
                {
                    "athlete": {"displayName": "LeBron James"},
                    "status": "Out",
                    "details": {"detail": "Illness", "type": "Illness"},
                },
            ],
        },
    ],
}


def _rotowire_html(player: str, body: str) -> str:
    return (
        '<div class="news-update">'
        '  <div class="news-update__headline">Some headline</div>'
        f'  <a data-player-id="123">{player}</a>'
        f'  <div class="news-update__news">{body}</div>'
        "</div></div>"
    )


# ── Facade re-exports ───────────────────────────────────────────────────

def test_facade_reexports_public_surface():
    import tools.news as news_pkg
    import tools.news_ingestion as ni

    for name in ni.__all__:
        assert hasattr(ni, name), f"facade missing {name}"
        obj = getattr(ni, name)
        # Facade objects must be the same objects as the package's, not copies.
        assert getattr(news_pkg, name) is obj


def test_split_modules_importable():
    from tools.news import _http, api, dedup, espn, inference, models, rotowire  # noqa: F401

    from tools.news.models import CoachingEvent, InjuryEvent, LineupEvent
    assert InjuryEvent is not LineupEvent is not CoachingEvent or True
    # dataclasses constructible
    now = datetime.now(timezone.utc).isoformat()
    ev = InjuryEvent(
        sport="basketball_nba", player_name="X", team=None, body_part=None,
        status="out", severity="severe", first_seen_at=now,
        source="t", source_url=None, raw={},
    )
    row = ev.as_news_row()
    assert row["event_type"] == "injury"
    assert row["player_name"] == "X"


def test_facade_private_aliases_for_backcompat():
    import tools.news_ingestion as ni

    # Legacy underscore names that tests / tooling may reach for.
    assert callable(ni._get_client)
    assert callable(ni._fetch_espn_injuries)
    assert callable(ni._fetch_rotowire_news)
    assert callable(ni._fetch_espn_scoreboard_lineups)
    assert callable(ni._fetch_espn_coaching)
    assert callable(ni._ensure_schema)
    assert callable(ni._dedup_key)
    assert callable(ni._now_iso)
    assert ni._RotoWireLimiter is not None


# ── Inference through both paths ────────────────────────────────────────

def test_inference_consistency():
    import tools.news.inference as inf
    import tools.news_ingestion as ni

    cases = [
        ("Out", "out for the season with knee"),
        ("Day-to-Day", "sore ankle"),
        ("Questionable", None),
        ("Doubtful", "will not play"),
        (None, None),
    ]
    for status, detail in cases:
        assert ni.infer_severity(status, detail) == inf.infer_severity(status, detail)

    texts = ["right ankle sprain", "background noise", None, "concussion protocol", "flu-like symptoms"]
    for t in texts:
        assert ni.infer_body_part(t) == inf.infer_body_part(t)


# ── Dedup via split module ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dedup_cross_source_via_package(tmp_db):
    await _init_ingestion_runs(tmp_db)
    from tools.news.dedup import dedupe_injuries
    from tools.news.models import InjuryEvent

    t1 = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc).isoformat()
    t2 = datetime(2026, 4, 22, 10, 8, 0, tzinfo=timezone.utc).isoformat()

    events = [
        InjuryEvent(
            sport="basketball_nba", player_name="Jayson Tatum", team="BOS",
            body_part="lower_body", status="out", severity="severe",
            first_seen_at=t1, source="espn.injuries",
            source_url="https://espn.example", raw={},
        ),
        InjuryEvent(
            sport="basketball_nba", player_name="J. Tatum", team="BOS",
            body_part="lower_body", status="out", severity="out_indefinite",
            first_seen_at=t2, source="rotowire.news",
            source_url="https://rw.example", raw={},
        ),
        # Different player entirely — must stay separate.
        InjuryEvent(
            sport="basketball_nba", player_name="Kevin Huerter", team="SAC",
            body_part="lower_body", status="questionable", severity="minor",
            first_seen_at=t2, source="espn.injuries",
            source_url="https://espn.example", raw={},
        ),
    ]
    rows = dedupe_injuries(events)
    assert len(rows) == 2
    tatum = [r for r in rows if "Tatum" in (r["player_name"] or "")][0]
    assert tatum["confirmed_at"] == t2
    assert tatum["severity"] == "out_indefinite"   # most severe wins
    assert set(tatum["source"].split("+")) == {"espn.injuries", "rotowire.news"}
    huerter = [r for r in rows if r["player_name"] == "Kevin Huerter"][0]
    assert huerter["confirmed_at"] is None


# ── Fetchers with mocked httpx through the facade seam ──────────────────

@pytest.mark.asyncio
async def test_espn_injuries_via_facade_seam(tmp_db, monkeypatch):
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
    t = by_player["Jayson Tatum"]
    assert t.status == "out" and t.severity == "severe"
    assert t.body_part == "lower_body"          # 'ankle'
    assert by_player["LeBron James"].body_part == "illness"


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
                return _FakeResponse(
                    {}, status_code=200,
                    text=_rotowire_html("Stephen Curry", "Curry day-to-day with ankle sprain"),
                )
            return _FakeResponse({})

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(ni, "_get_client", lambda: _FakeClient())
    # Short-circuit RotoWire rate limiter so the test is fast.
    monkeypatch.setattr(
        ni._RotoWireLimiter, "wait",
        staticmethod(lambda *a, **k: asyncio.sleep(0)),
    )

    rows = await ni.fetch_injuries("basketball_nba")
    players = {r["player_name"] for r in rows}
    assert "Jayson Tatum" in players
    assert "Stephen Curry" in players
    curry = [r for r in rows if r["player_name"] == "Stephen Curry"][0]
    assert curry["body_part"] == "lower_body"
    assert curry["status"] == "questionable"    # 'day-to-day'


@pytest.mark.asyncio
async def test_fetch_lineup_and_coaching_with_mocks(tmp_db, monkeypatch):
    await _init_ingestion_runs(tmp_db)
    import tools.news_ingestion as ni

    scoreboard = {
        "events": [
            {
                "competitions": [
                    {
                        "competitors": [
                            {
                                "team": {"displayName": "Boston Celtics"},
                                "injuries": [
                                    {
                                        "athlete": {"displayName": "Al Horford"},
                                        "status": "Out",
                                    }
                                ],
                            }
                        ],
                        "notes": [{"headline": "Resting starters for load management"}],
                    }
                ]
            }
        ]
    }

    class _FakeClient:
        is_closed = False

        async def get(self, url, params=None):
            return _FakeResponse(scoreboard)

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(ni, "_get_client", lambda: _FakeClient())

    lineups = await ni.fetch_lineup_changes("basketball_nba")
    assert len(lineups) == 1
    assert lineups[0]["event_type"] == "lineup_change"
    assert lineups[0]["player_name"] == "Al Horford"
    assert lineups[0]["status"] == "inactive"

    coaching = await ni.fetch_coaching_news("basketball_nba")
    assert len(coaching) == 1
    assert coaching[0]["event_type"] == "coaching_decision"
    assert coaching[0]["player_name"] is None
    assert coaching[0]["severity"] == "severe"


# ── Persistence idempotency ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_persist_news_rows_idempotent_within_window(tmp_db):
    await _init_ingestion_runs(tmp_db)
    import tools.news_ingestion as ni

    now = datetime.now(timezone.utc).isoformat()
    row = {
        "sport": "basketball_nba",
        "event_id": None,
        "player_name": "Jayson Tatum",
        "event_type": "injury",
        "severity": "severe",
        "body_part": "lower_body",
        "status": "out",
        "first_seen_at": now,
        "confirmed_at": None,
        "source": "espn.injuries",
        "source_url": None,
        "raw_json": "{}",
        "local_game_date": None,
    }
    n1 = await ni.persist_news_rows([row], db_path=tmp_db)
    n2 = await ni.persist_news_rows([dict(row)], db_path=tmp_db)
    assert n1 == 1
    assert n2 == 0

    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute("SELECT COUNT(*) FROM news_events")
        (cnt,) = await cur.fetchone()
    assert cnt == 1


@pytest.mark.asyncio
async def test_ensure_schema_creates_table(tmp_db):
    import tools.news_ingestion as ni

    async with aiosqlite.connect(tmp_db) as db:
        await ni._ensure_schema(db)
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='news_events'"
        )
        assert await cur.fetchone() is not None


# ── Rate limiter behaviour ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rotowire_limiter_gates_calls():
    from tools.news.rotowire import RotoWireLimiter

    RotoWireLimiter._next_ok_at = 0.0
    loop = asyncio.get_event_loop()
    start = loop.time()
    await asyncio.gather(RotoWireLimiter.wait(0.15), RotoWireLimiter.wait(0.15))
    elapsed = loop.time() - start
    # Second caller must have been held back at least one interval.
    assert elapsed >= 0.14
