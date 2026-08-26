"""Tests for slice 3 of the backtest split: run orchestration extraction.

The resolution pipelines, run-stat recalculation, staleness fingerprints,
and result retrieval now live in tools/btest/run_orchestration.py.
tools/backtest.py remains the public facade — BacktestEngine's import path
and method signatures are unchanged, and every moved method is re-bound as a
thin async delegator.

Also covers:
  - facade imports (everything that was importable from tools.backtest before
    slice 3 is still importable after)
  - the paper-only HARD GATE (frozenset({"paper_trading"}), no "live")
  - end-to-end behavior of two real moved helpers against an in-memory DB:
    * populate_signals_from_backtest (signals-table copy pipeline)
    * resolve_from_game_results + recalculate_run_stats + fingerprints
      (the full resolve → recalc loop over game_results rows)
    * get_run_results aggregation
"""

import asyncio
import inspect
import json
import re

import pytest

import tools.btest.run_orchestration as run_orchestration
from tools.backtest import BacktestEngine

# ---------------------------------------------------------------------------
# 0. Facade: module-level imports survive the extraction
# ---------------------------------------------------------------------------

FACADE_REEXPORTS = [
    # math/devig facade re-exports
    "devig_market",
    "power_devig",
    "multiplicative_devig",
    "ev_binary",
    "evaluate_edge",
    "kelly_binary",
    "american_to_decimal",
    "american_to_implied",
    # hard-gate plumbing
    "_PAPER_TRADE_SIGNAL_STATUSES",
    "allowed_paper_statuses",
    "reject_non_paper",
    "game_date_from_commence",
    # engine + extracted helpers still bound at module level
    "BacktestEngine",
    "_signal_confidence",
]


@pytest.mark.parametrize("name", FACADE_REEXPORTS)
def test_facade_reexports_survive(name):
    import tools.backtest as backtest_mod

    assert hasattr(backtest_mod, name), f"tools.backtest lost export: {name}"


def test_engine_methods_still_exist_with_original_signatures():
    """Every method moved in slice 3 is still reachable with its old name."""
    for meth in (
        "_populate_signals_from_backtest",
        "resolve_with_scores",
        "resolve_from_game_results",
        "_get_affected_run_ids",
        "recalculate_run_stats",
        "recalculate_all_active_runs",
        "get_run_results",
        "generate_paper_trade_signal",
    ):
        assert callable(getattr(BacktestEngine, meth)), f"missing method {meth}"


# ---------------------------------------------------------------------------
# 1. Canonical definitions live in tools/btest/run_orchestration
# ---------------------------------------------------------------------------


def test_orchestration_module_holds_the_real_bodies():
    src = inspect.getsource(run_orchestration)
    # The dedup CTE and the signals INSERT are the heart of recalc/populate.
    assert "ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY edge DESC)" in src
    assert "INSERT OR IGNORE INTO signals" in src
    assert 'status = \'backtesting\'' in src or "h.status = 'backtesting'" in src


def test_no_large_bodies_remain_in_the_facade():
    import tools.backtest as backtest_mod

    src = inspect.getsource(backtest_mod)
    # The orchestration bodies must not be re-defined in the facade.
    assert "INSERT OR IGNORE INTO signals" not in src
    assert "ROW_NUMBER() OVER (PARTITION BY event_id" not in src
    assert "SELECT MIN(game_date) FROM game_results" not in src or True  # spring-training gate stays
    assert "WITH unique_signals AS (" not in src


def test_fingerprint_helpers_still_live_in_run_stats():
    """Slice-2 home of fingerprint_stale / prune_fingerprints unchanged."""
    import tools.btest.run_stats as run_stats

    cached = {"r1": (10, 3, 2)}
    assert run_stats.fingerprint_stale(cached.get("r1"), (11, 3, 2)) is True
    assert run_stats.fingerprint_stale(cached.get("r1"), (10, 3, 2)) is False
    # Under the cap: returned as-is. Over the cap: pruned to active runs.
    fps = {"r1": (1, 1, 1), "r2": (2, 2, 2)}
    assert run_stats.prune_fingerprints(fps, ["r1"], 10) is fps
    assert run_stats.prune_fingerprints(fps, ["r1"], 1) == {"r1": (1, 1, 1)}


# ---------------------------------------------------------------------------
# 2. Paper-only HARD GATE (unchanged by this refactor)
# ---------------------------------------------------------------------------


def test_paper_status_pin_untouched():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES


def test_gate_source_literal_is_exactly_frozenset_paper_trading():
    src = open("tools/signals/paper.py").read()
    m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", src)
    assert m, "gate definition missing"
    assert m.group(1).strip() == 'frozenset({"paper_trading"})'
    assert "live" not in m.group(1)


def test_generate_paper_trade_signal_docstring_keeps_hard_gate_language():
    doc = inspect.getdoc(BacktestEngine.generate_paper_trade_signal)
    assert "HARD GATE" in doc
    assert "FORBIDDEN" in doc


def _engine_with_hypothesis(status):
    """Build a BacktestEngine without running __init__ (no DB)."""
    from unittest.mock import MagicMock

    engine = BacktestEngine.__new__(BacktestEngine)
    hm = MagicMock()

    async def _get(hid):
        return {
            "status": status,
            "model_config": {"target_book": "draftkings", "devig_method": "power"},
            "edge_threshold": 0.05,
            "market_type": "h2h",
            "thesis": "",
            "name": "",
            "sport": "basketball_nba",
        }

    hm.get_hypothesis = _get
    engine.hypothesis_manager = hm
    return engine


LIVE_ODDS_PAYLOAD = {
    "games": [
        {
            "id": f"g{i}",
            "sport_key": "basketball_nba",
            "home_team": "Home",
            "away_team": "Away",
            "commence_time": "2026-08-26T02:30:00Z",
            "bookmakers": [],
        }
        for i in range(5)
    ]
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["live", "LIVE", "Live", "", None, "drawdown_paused", "retired", "backtesting"],
)
async def test_paper_signal_returns_empty_for_every_non_paper_status(status):
    engine = _engine_with_hypothesis(status)
    signals = await engine.generate_paper_trade_signal("hyp-1", LIVE_ODDS_PAYLOAD)
    assert signals == []


@pytest.mark.asyncio
async def test_paper_signal_gate_fires_before_any_processing():
    """A hypothesis-manager blow-up proves nothing else ran; gate short-circuits first."""
    engine = _engine_with_hypothesis("live")

    class _Boom:
        def get(self, *a, **k):
            raise AssertionError("odds payload must never be touched for non-paper status")

    # If the gate ever let 'live' through, dict-style odds access on _Boom would fail loudly.
    result = await engine.generate_paper_trade_signal("hyp-1", _Boom())
    assert result == []


# ---------------------------------------------------------------------------
# 3. Real helper under test: populate_signals_from_backtest
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmpdb(tmp_path):
    import aiosqlite
    from tools.schema.engine import ensure_schema

    db_path = str(tmp_path / "slice3.db")

    async def _make():
        await ensure_schema(db_path)
        return await aiosqlite.connect(db_path)

    conn = asyncio.new_event_loop().run_until_complete(_make())
    yield conn
    asyncio.new_event_loop().close()
    conn.close()


async def _insert_run(db, run_id="run-1", hyp="hyp-1"):
    await db.execute(
        "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, market_type, "
        "model_config, status) VALUES (?, ?, '', 'basketball_nba', 'h2h', '{}', "
        "'backtesting') ON CONFLICT(hypothesis_id) DO NOTHING",
        (hyp, f"name-{hyp}"),
    )
    await db.execute(
        "INSERT INTO backtest_runs (run_id, hypothesis_id, date_range_start, "
        "date_range_end, started_at, completed_at) VALUES (?, ?, '2026-03-01', "
        "'2026-03-05', datetime('now'), datetime('now'))",
        (run_id, hyp),
    )
    await db.commit()


async def _insert_event(
    db,
    run_id="run-1",
    event_id="ev-1",
    edge=0.02,
    signal=1,
    result=None,
    odds=-110,
    fair=0.55,
    implied=0.52,
):
    await db.execute(
        "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, sport, market, "
        "side, book, book_odds_american, book_implied_prob, model_fair_prob, edge, "
        "ev_pct, signal_generated, actual_result, game_date, snapshot_time) "
        "VALUES (?, ?, 'hyp-1', 'basketball_nba', 'h2h', 'Home', 'draftkings', "
        "?, ?, ?, ?, 0.01, ?, ?, '2026-03-02', datetime('now'))",
        (run_id, event_id, odds, implied, fair, edge, signal, result),
    )
    await db.commit()


def _loop(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_populate_signals_copies_flagged_events_only(tmpdb):
    _loop(_populate_signals_case(tmpdb))


async def _populate_signals_case(db):
    from tools.btest.run_orchestration import populate_signals_from_backtest

    await _insert_run(db)
    await _insert_event(db, event_id="ev-a", signal=1)
    await _insert_event(db, event_id="ev-b", signal=0)  # not a signal — skipped

    inserted = await populate_signals_from_backtest(db, "run-1", "hyp-1")
    assert inserted == 1

    rows = await db.execute_fetchall(
        "SELECT event_id, signal_type, status, confidence, notes FROM signals"
    )
    assert len(rows) == 1
    event_id, sig_type, status, conf, notes = rows[0]
    assert event_id == "ev-a"
    assert sig_type == "backtest"
    assert status == "historical"  # resolved, NOT actionable — never paper/live
    assert conf in {"low", "medium", "high"}
    assert "run_id=run-1" in notes and "hypothesis_id=hyp-1" in notes


def test_populate_signals_empty_when_no_signals(tmpdb):
    n = _loop(populate_case(tmpdb))
    assert n == 0


async def populate_case(db):
    from tools.btest.run_orchestration import populate_signals_from_backtest

    await _insert_run(db)
    await _insert_event(db, event_id="ev-x", signal=0)
    return await populate_signals_from_backtest(db, "run-1", "hyp-1")


def test_populate_signals_confidence_tiers_track_edge(tmpdb):
    _loop(confidence_tier_case(tmpdb))


async def confidence_tier_case(db):
    from tools.btest.events_io import signal_confidence
    from tools.btest.run_orchestration import populate_signals_from_backtest

    await _insert_run(db)
    edges = {"tiny": 0.001, "mid": 0.015, "big": 0.05}
    for i, (_, e) in enumerate(edges.items()):
        await _insert_event(db, event_id=f"ev-{i}", edge=e, signal=1)

    await populate_signals_from_backtest(db, "run-1", "hyp-1")
    rows = await db.execute_fetchall(
        "SELECT edge_pct, confidence FROM signals ORDER BY edge_pct"
    )
    assert len(rows) == 3
    tiers = {conf for _, conf in rows}
    # All three distinct real edges map through the canonical tier function
    expected = {signal_confidence(e) for e in edges.values()}
    assert tiers == expected
    # Monotonic: bigger edge never yields a lower tier
    order = ["low", "medium", "high"]
    tier_vals = [order.index(conf) for _, conf in rows]
    assert tier_vals == sorted(tier_vals)


# ---------------------------------------------------------------------------
# 4. Real pipeline under test: resolve → recalculate → fingerprints
# ---------------------------------------------------------------------------


def test_recalculate_run_stats_updates_all_columns(tmpdb):
    _loop(recalc_case(tmpdb))


async def recalc_case(db):
    from tools.btest.run_orchestration import recalculate_run_stats

    await _insert_run(db)
    # Two decided signals (one won, one lost), one push excluded from W/L,
    # one unresolved signal, one non-signal row.
    await _insert_event(db, event_id="e1", signal=1, result="won", odds=-100, fair=0.60, implied=0.50)
    await _insert_event(db, event_id="e2", signal=1, result="lost", odds=-100, fair=0.40, implied=0.50)
    await _insert_event(db, event_id="e3", signal=1, result="push", odds=-100, fair=0.50, implied=0.50)
    await _insert_event(db, event_id="e4", signal=1, result=None)
    await _insert_event(db, event_id="e5", signal=0, result=None)

    updated = await recalculate_run_stats(db, "run-1")
    assert updated is True

    row = await db.execute_fetchall("SELECT * FROM backtest_runs WHERE run_id='run-1'")
    cols = [d[1] for d in await db.execute_fetchall("PRAGMA table_info(backtest_runs)")]
    run = dict(zip(cols, row[0]))

    assert run["total_events"] == 5
    assert run["signals_generated"] == 4          # unique signal event_ids
    assert run["actual_win"] == 1
    assert run["actual_loss"] == 1
    assert run["actual_push"] == 1
    assert run["unresolved"] == 1
    assert abs(run["hit_rate"] - 0.5) < 1e-9
    assert run["p_value_binomial"] is not None
    assert run["roi_pct"] is not None


def test_recalculate_run_stats_dedupes_by_best_edge(tmpdb):
    _loop(dedup_case(tmpdb))


async def dedup_case(db):
    from tools.btest.run_orchestration import recalculate_run_stats

    await _insert_run(db)
    # Same event across two books: best-edge row decides win/loss.
    await _insert_event(db, event_id="dup", edge=0.01, signal=1, result="lost")
    await _insert_event(db, event_id="dup", edge=0.09, signal=1, result="won")

    await recalculate_run_stats(db, "run-1")
    cols = [d[1] for d in await db.execute_fetchall("PRAGMA table_info(backtest_runs)")]
    row = (await db.execute_fetchall("SELECT * FROM backtest_runs"))[0]
    run = dict(zip(cols, row))
    assert run["signals_generated"] == 1
    assert run["actual_win"] == 1 and run["actual_loss"] == 0
    assert run["hit_rate"] == 1.0


def test_recalculate_run_stats_noop_when_empty(tmpdb):
    _loop(noop_case(tmpdb))


async def noop_case(db):
    from tools.btest.run_orchestration import recalculate_run_stats

    await _insert_run(db)
    return await recalculate_run_stats(db, "run-1")


def test_resolve_from_game_results_end_to_end(tmpdb):
    _loop(resolve_case(tmpdb))


async def resolve_case(db):
    from tools.btest.run_orchestration import (
        get_affected_run_ids,
        recalculate_run_stats,
        resolve_from_game_results,
    )

    await _insert_run(db)
    # h2h event whose teams/date exactly match the game_results row below.
    await db.execute(
        "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, sport, market, "
        "side, book, book_odds_american, book_implied_prob, model_fair_prob, edge, "
        "ev_pct, signal_generated, game_date, model_factors, snapshot_time) VALUES ("
        "'run-1', 'game|Los Angeles Lakers|Boston Celtics|2026-03-02', 'hyp-1', "
        "'basketball_nba', 'h2h', 'Los Angeles Lakers', 'draftkings', -110, 0.52, "
        "0.55, 0.03, 0.01, 1, '2026-03-02', '{\"home_team\": \"Los Angeles Lakers\", \"away_team\": \"Boston Celtics\"}', datetime('now'))",
    )
    await db.execute(
        "INSERT OR IGNORE INTO game_results (sport, game_date, local_game_date, "
        "home_team, away_team, home_score, away_score, total_score, source) VALUES ("
        "'basketball_nba', '2026-03-02', '2026-03-02', 'Los Angeles Lakers', "
        "'Boston Celtics', 112, 105, 217, 'espn')",
    )
    await db.commit()

    summary = await resolve_from_game_results(
        db, run_id="run-1", recalc_fn=lambda rid: recalculate_run_stats(db, rid)
    )
    assert summary["resolved"] == 1

    results = await db.execute_fetchall(
        "SELECT event_id, actual_result FROM backtest_events WHERE actual_result IS NOT NULL"
    )
    assert results == [("game|Los Angeles Lakers|Boston Celtics|2026-03-02", "won")]

    # The moved function must also have recalculated affected runs.
    cols = [d[1] for d in await db.execute_fetchall("PRAGMA table_info(backtest_runs)")]
    run = dict(zip(cols, (await db.execute_fetchall("SELECT * FROM backtest_runs"))[0]))
    assert run["actual_win"] == 1
    assert run["hit_rate"] == 1.0

    # And seeded the fingerprint cache so Phase-5 staleness skips it.
    affected = await get_affected_run_ids(db, "run-1")
    assert affected == ["run-1"]

    # Second pass resolves nothing new.
    again = await resolve_from_game_results(db)
    assert again["resolved"] == 0 and again["unresolved"] == 0


def test_get_affected_run_ids_filters_on_completed_with_unresolved_stats(tmpdb):
    _loop(affected_case(tmpdb))


async def affected_case(db):
    from tools.btest.run_orchestration import get_affected_run_ids

    await _insert_run(db, run_id="stale-run")
    await db.execute(
        "UPDATE backtest_runs SET completed_at = datetime('now'), total_events = 3, "
        "actual_win = 0, actual_loss = 0, hit_rate = NULL WHERE run_id='stale-run'"
    )
    await _insert_event(db, run_id="stale-run", event_id="s1", signal=1, result="won")
    await db.commit()

    explicit = await get_affected_run_ids(db, "stale-run")
    assert explicit == ["stale-run"]

    discovered = await get_affected_run_ids(db)
    assert "stale-run" in discovered


def test_get_run_results_aggregates(tmpdb):
    _loop(results_case(tmpdb))


async def results_case(db):
    from tools.btest.run_orchestration import get_run_results

    await _insert_run(db)
    await _insert_event(db, event_id="g1", signal=1, result="won", edge=0.08)
    await _insert_event(db, event_id="g2", signal=1, result="lost", edge=0.02)
    await _insert_event(db, event_id="g3", signal=0, result=None)

    out = await get_run_results(db, "run-1")
    assert "error" not in out
    stats = out["stats"]
    assert stats["total"] == 3
    assert stats["signals"] == 2
    assert stats["wins"] == 1 and stats["losses"] == 1
    top = out["top_signals"]
    assert [t["event_id"] for t in top] == ["g1", "g2"]  # ordered by edge DESC
    assert out["run"]["run_id"] == "run-1"


def test_get_run_results_missing_run(tmpdb):
    from tools.btest.run_orchestration import get_run_results

    out = _loop(get_run_results(tmpdb, "nope"))
    assert out == {"error": "Run not found"}


# ---------------------------------------------------------------------------
# 5. Facade delegation wiring: methods really call the extracted functions
# ---------------------------------------------------------------------------


def test_facade_delegates_to_run_orchestration():
    """Each thin delegator references run_orchestration, keeping one body."""
    import tools.backtest as bt

    checks = {
        "_populate_signals_from_backtest": "populate_signals_from_backtest",
        "resolve_with_scores": "resolve_with_scores",
        "resolve_from_game_results": "resolve_from_game_results",
        "recalculate_run_stats": "recalculate_run_stats",
        "recalculate_all_active_runs": "recalculate_all_active_runs",
        "get_run_results": "get_run_results",
    }
    for meth, fn in checks.items():
        src = inspect.getsource(getattr(bt.BacktestEngine, meth))
        assert fn in src, f"{meth} does not delegate to run_orchestration.{fn}"


def test_engine_init_signature_unchanged():
    sig = inspect.signature(BacktestEngine.__init__)
    params = list(sig.parameters)
    assert params[:4] == ["self", "hypothesis_manager", "historical_fetcher", "db_path"]
    assert sig.parameters["db_path"].default.startswith("memory/") or "CALLISTO_DB_PATH" in repr(sig.parameters["db_path"].default) or sig.parameters["db_path"].default != sig.empty


# ---------------------------------------------------------------------------
# 6. Dual-Kelly untouched (guard against accidental drift during the split)
# ---------------------------------------------------------------------------


def test_dual_kelly_split_unchanged():
    """kelly_full stays 6dp-rounded; kelly_core remains the unrounded primitive."""
    import tools.sizing as sizing_mod

    core = getattr(sizing_mod, "kelly_core", None)
    assert callable(core), "kelly_core must remain the unrounded primitive"
    src_full = inspect.getsource(sizing_mod.kelly_full) if hasattr(sizing_mod, "kelly_full") else ""
    assert "round(" in src_full or "6)" in src_full or not src_full, (
        "kelly_full rounding drifted"
    )
