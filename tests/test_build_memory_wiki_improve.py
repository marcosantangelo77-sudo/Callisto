"""improve/memory-wiki — decay must survive into prompt context.

Defect measured before the fix (family #1: a policy layer that never runs):
_build_learnings computed decay_confidence(...) and passed it as
effective_confidence, but annotate_for_reinjection OVERWROTE it from the raw
stored confidence. A learning recorded 100 days earlier emitted
"[eff 55% conf ...]" in the live prompt where the documented decay policy says
~5%. Decay also never influenced trimming priority for the same reason.

These tests pin the fixed behaviour END-TO-END through HermesMemory's real SQL
and section builder, not just the pure function.
"""
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("CALLISTO_DB_PATH", ":memory:")

from tools.memory_epistemics import (
    MIN_EFFECTIVE_CONFIDENCE,
    PROVENANCE_CEILINGS,
    annotate_for_reinjection,
    decay_confidence,
)

NOW = datetime.now(timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class TestAnnotateHonorsProvidedEffective:
    def test_provided_effective_survives(self):
        row = {"key": "k", "confidence": 0.55, "effective_confidence": 0.05}
        out = annotate_for_reinjection(row)
        assert out["effective_confidence"] == pytest.approx(0.05)

    @pytest.mark.parametrize("_", range(50))
    def test_provided_effective_never_exceeds_ceiling_random(self, _):
        cls = "INFERRED"
        eff = 0.55 + (hash((_)) % 100) / 100 * 0.45  # deterministic spread > ceiling
        out = annotate_for_reinjection(
            {"key": "k", "confidence": 0.55, "source_class": cls,
             "effective_confidence": eff})
        assert out["effective_confidence"] <= PROVENANCE_CEILINGS[cls]

    def test_fallback_without_effective_unchanged(self):
        """Pins the pre-existing contract relied on by
        tests/test_redteam_prov_memory_wiki.py: raw confidence capped by class."""
        out = annotate_for_reinjection({"key": "k", "confidence": 0.9})
        assert out["source_class"] == "INFERRED"
        assert out["effective_confidence"] == pytest.approx(
            min(0.9, PROVENANCE_CEILINGS["INFERRED"]))


# ── End-to-end: the prompt path ────────────────────────────────────────────

SPORTS_SCHEMA = [
    "(timestamp TEXT, balance REAL)",                                   # bankroll
    ("(id INTEGER PRIMARY KEY, game_description TEXT, team TEXT, market TEXT,"
     " bookmaker TEXT, placement_odds INTEGER, stake REAL, payout REAL,"
     " result TEXT, clv_implied REAL, placed_at TEXT, notes TEXT)"),     # bets
    ("(sport TEXT, team TEXT, market TEXT, bookmaker TEXT, american_odds INTEGER,"
     " edge REAL, expected_value REAL, kelly_fraction REAL, detected_at TEXT)"),  # ev_opportunities
    ("(session_id TEXT PRIMARY KEY, query TEXT, conclusion TEXT,"
     " confidence_score REAL, confidence_tier TEXT, sealed_at TEXT)"),   # sessions
    ("(hypothesis_id INTEGER PRIMARY KEY, name TEXT, sport TEXT, market_type TEXT,"
     " thesis TEXT, status TEXT, updated_at TEXT)"),                     # hypotheses
    ("(event_id INTEGER PRIMARY KEY, hypothesis_id INTEGER,"
     " signal_generated INTEGER, edge REAL)"),                           # backtest_events
]


def _make_db(tmp_path, learnings):
    db_path = str(Path(tmp_path) / "probe.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE hermes_learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE, value TEXT NOT NULL,
            learned_at TEXT NOT NULL, confidence REAL DEFAULT 0.5,
            occurrences INTEGER DEFAULT 1, source TEXT DEFAULT 'claude')""")
    conn.execute(
        """CREATE TABLE hermes_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            sender TEXT NOT NULL, message TEXT NOT NULL, read INTEGER DEFAULT 0)""")
    for table, ddl in zip(
        ("bankroll", "bets", "ev_opportunities", "sessions",
         "hypotheses", "backtest_events"), SPORTS_SCHEMA):
        conn.execute(f"CREATE TABLE {table} {ddl}")
    for key, value, learned_at, conf in learnings:
        conn.execute(
            "INSERT INTO hermes_learnings (key,value,learned_at,confidence,source)"
            " VALUES (?,?,?,?, 'claude')", (key, value, learned_at, conf))
    conn.commit()
    conn.close()
    return db_path


def _learnings_section(ctx: str) -> str:
    m = re.search(r'<memory type="learnings">(.*?)</memory>', ctx, re.S)
    return m.group(1) if m else ""


def _emitted_eff(section: str, key: str):
    m = re.search(rf"\[eff (\d+)% conf[^\]]*\] {re.escape(key)}:", section)
    return int(m.group(1)) if m else None


class TestDecayReachesThePrompt:
    def test_stale_learning_emits_decayed_confidence(self, tmp_path):
        """THE regression: a 100-day-old INFERRED learning at stored 0.55
        must emit ~floor effective confidence, not its raw 55%."""
        db_path = _make_db(tmp_path, [
            ("stale_guess", "stale claim written long ago", _iso(100), 0.55),
        ])
        from tools.hermes_memory import HermesMemory
        hm = HermesMemory(db_path=db_path)
        ctx = asyncio.run(hm.get_memory_context(force_refresh=True))
        section = _learnings_section(ctx)
        assert section, "learnings section missing entirely"
        eff = _emitted_eff(section, "stale_guess")
        assert eff is not None, f"stale_guess not rendered:\n{section}"
        true_eff = float(decay_confidence(
            0.55, _iso(100), datetime.now(timezone.utc)))
        assert eff <= max(true_eff, MIN_EFFECTIVE_CONFIDENCE) * 100 + 1, (
            f"emitted {eff}% but decayed truth is ~{true_eff:.0%} "
            "- decay did not reach the prompt")

    def test_fresh_learning_keeps_full_capped_confidence(self, tmp_path):
        db_path = _make_db(tmp_path, [
            ("fresh_guess", "fresh claim", _iso(0), 0.55),
        ])
        from tools.hermes_memory import HermesMemory
        hm = HermesMemory(db_path=db_path)
        ctx = asyncio.run(hm.get_memory_context(force_refresh=True))
        eff = _emitted_eff(_learnings_section(ctx), "fresh_guess")
        assert eff is not None
        assert eff >= 54  # ~ceiling, not decayed (fresh observation)

    def test_stale_loses_trim_priority_to_fresh_under_budget(self, tmp_path):
        """Trimming ranks on effective_confidence; post-fix that is the DECAYED
        value, so a stale learning is dropped before any fresh one.
        _build_learnings keeps 10; 10 fresh + 1 stale makes the budget bind."""
        learnings = [(f"fresh_{i:02d}", "v", _iso(0), 0.55) for i in range(10)]
        learnings.append(("stale_guess", "v", _iso(200), 0.55))
        db_path = _make_db(tmp_path, learnings)
        from tools.hermes_memory import HermesMemory
        hm = HermesMemory(db_path=db_path)
        ctx = asyncio.run(hm.get_memory_context(force_refresh=True))
        section = _learnings_section(ctx)
        kept_fresh = [f"fresh_{i:02d}" for i in range(10) if f"fresh_{i:02d}" in section]
        assert len(kept_fresh) == 10, f"budget should keep all fresh, got {len(kept_fresh)}"
        assert "stale_guess" not in section, (
            "a 200-day-stale learning survived trimming over fresh items - "
            "ranking is still using undecayed confidence")

    @pytest.mark.parametrize("age_days", [0, 7, 14, 28, 56, 100, 365])
    def test_emitted_confidence_monotone_nonincreasing_in_age(self, tmp_path,
                                                              age_days):
        """Property sweep across ages: older never emits higher than younger."""
        db_path = _make_db(tmp_path, [
            ("aged", "claim", _iso(age_days), 0.55)])
        from tools.hermes_memory import HermesMemory
        hm = HermesMemory(db_path=db_path)
        ctx = asyncio.run(hm.get_memory_context(force_refresh=True))
        eff = _emitted_eff(_learnings_section(ctx), "aged")
        assert eff is not None
        cap = 55  # INFERRED ceiling in percent
        floor_pct = MIN_EFFECTIVE_CONFIDENCE * 100
        assert floor_pct - 1 <= eff <= cap
        if age_days > 0:
            expected = float(decay_confidence(
                0.55, _iso(age_days), datetime.now(timezone.utc)))
            assert eff / 100 <= max(expected, MIN_EFFECTIVE_CONFIDENCE) + 0.01
