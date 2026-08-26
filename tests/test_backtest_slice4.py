"""Tests for slice 4 of the backtest split: pipeline extraction.

Slice 4 moves three more large bodies out of tools/backtest.py into
tools/btest/:
  - run_pipeline.run_backtest        (the full replay loop: gates, fetch,
                                     per-game processing, deferred batch
                                     write, resolution + significance)
  - prop_processing.process_game_props / process_prop_snapshots
                                     (player-prop event generation)
  - paper_pipeline.generate_paper_trade_signal
                                     (paper-trade body; the HARD GATE
                                     stays on the facade method)

tools/backtest.py remains the public facade — BacktestEngine re-binds all
of it as thin delegators, so call sites and signatures are unchanged.

Covered here:
  - facade imports (everything importable before slice 4 survives)
  - the canonical gate definition lives in tools/signals/paper.py and is
    exactly frozenset({"paper_trading"}) — never "live"
  - generate_paper_trade_signal rejects every non-paper status BEFORE any
    processing (gate precedes the extracted body)
  - the extracted run_pipeline gates behave identically through the facade:
    spring-training skip, side_filter_required, date-range safety
  - a real moved helper exercised end-to-end against an on-disk DB:
    prop_processing.process_prop_snapshots (devig → edge → signal → row
    insert), including its MAX_EDGE_MAGNITUDE clip via prop_processing.clip_edge
"""

import asyncio
import inspect
import json
import re

import pytest

import tools.btest.paper_pipeline as paper_pipeline
import tools.btest.prop_processing as prop_processing
import tools.btest.run_pipeline as run_pipeline
from tools.backtest import BacktestEngine

# ---------------------------------------------------------------------------
# 0. Facade: module-level imports survive the extraction
# ---------------------------------------------------------------------------

FACADE_REEXPORTS = [
    # math/devig/ev/sizing re-exports
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
    # engine + helpers bound at module level across all slices
    "BacktestEngine",
    "_signal_confidence",
]


@pytest.mark.parametrize("name", FACADE_REEXPORTS)
def test_facade_reexports_survive_slice4(name):
    import tools.backtest as backtest_mod

    assert hasattr(backtest_mod, name), f"tools.backtest lost export: {name}"


def test_engine_methods_still_exist_after_slice4():
    """Every public/private method reachable before slice 4 still resolves."""
    for meth in (
        "run_backtest",
        "_process_game",
        "_process_game_lines",
        "_process_game_props",
        "_process_prop_snapshots",
        "generate_paper_trade_signal",
        "resolve_with_scores",
        "resolve_from_game_results",
        "recalculate_run_stats",
        "recalculate_all_active_runs",
        "_populate_signals_from_backtest",
        "get_run_results",
        "_enrich_snapshot_with_multibook",
        "_parse_hypothesis_filters",
        "compute_context_coverage",
        "has_structured_filters",
    ):
        assert callable(getattr(BacktestEngine, meth)), f"missing method {meth}"


def test_extracted_modules_hold_the_real_bodies():
    """The big bodies are defined in tools/btest, not the facade."""
    facade_src = inspect.getsource(__import__("tools.backtest", fromlist=["x"]))
    pipe_src = inspect.getsource(run_pipeline)
    props_src = inspect.getsource(prop_processing)
    paper_src = inspect.getsource(paper_pipeline)

    # The full run pipeline lives in run_pipeline.
    assert "async def run_backtest(" in pipe_src
    assert "INSERT OR REPLACE INTO backtest_runs" in pipe_src
    assert "side_filter_required" in pipe_src

    # Prop processing lives in prop_processing with its constants.
    assert "async def process_game_props(" in props_src
    assert "async def process_prop_snapshots(" in props_src
    assert "MAX_EDGE_MAGNITUDE" in props_src

    # Paper-trade write path lives in paper_pipeline.
    assert "INSERT OR IGNORE INTO paper_trades" in paper_src
    assert "DELETE FROM backtest_events WHERE run_id = 'paper'" in paper_src

    # And the facade no longer defines any of them inline.
    assert "async def run_backtest(" not in facade_src.split("class BacktestEngine")[0]
    assert "INSERT OR IGNORE INTO paper_trades" not in facade_src


def test_facade_run_backtest_delegates_to_extracted_pipeline():
    src = inspect.getsource(BacktestEngine.run_backtest)
    assert "run_pipeline.run_backtest(" in src
    doc = inspect.getdoc(BacktestEngine.run_backtest)
    assert "tools.btest.run_pipeline" in doc


def test_kelly_semantics_untouched_by_extraction():
    """Dual Kelly pin: kelly_binary stays unrounded via kelly_core; the
    6dp rounding happens only at row-build time. Extraction must not have
    introduced rounding inside sizing."""
    import tools.sizing as sizing
    src = inspect.getsource(sizing)
    # No round(..., 6) inside kelly_binary itself.
    m = re.search(r"def kelly_binary\(.*?\n(?=\ndef |\Z)", src, re.S)
    assert m, "kelly_binary missing"
    assert "round(" not in m.group(0)


# ---------------------------------------------------------------------------
# 1. Paper-only HARD GATE (canonical source unchanged)
# ---------------------------------------------------------------------------


def test_paper_status_pin_untouched_slice4():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES


def test_gate_source_literal_is_exactly_frozenset_paper_trading_slice4():
    src = open("tools/signals/paper.py").read()
    m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", src)
    assert m, "gate definition missing"
    assert m.group(1).strip() == 'frozenset({"paper_trading"})'
    assert "live" not in m.group(1)


def test_gate_stays_on_the_facade_method_not_the_extracted_body():
    """The status check must run BEFORE the extracted pipeline body."""
    facade_src = inspect.getsource(BacktestEngine.generate_paper_trade_signal)
    assert "reject_non_paper" in facade_src
    assert 'return []' in facade_src
    # The extracted body does NOT re-check or widen statuses — it trusts
    # the gate and contains no status literals at all.
    body_src = inspect.getsource(paper_pipeline)
    assert '"live"' not in body_src
    assert "'live'" not in body_src


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
            "model_config": {"target_book": "draftkings", "devig_method": "power",
                             "side_filter": "Over"},
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
async def test_paper_signal_gate_fires_before_extracted_body_runs():
    """If the gate ever let 'live' through, odds access on this payload
    object would blow up inside the extracted body — proving the gate
    short-circuits before any processing."""
    engine = _engine_with_hypothesis("live")

    class _Boom:
        def get(self, *a, **k):
            raise AssertionError(
                "odds payload must never be touched for non-paper status"
            )

    result = await engine.generate_paper_trade_signal("hyp-1", _Boom())
    assert result == []


def test_paper_pipeline_module_imports_cleanly_and_is_used_by_facade():
    import tools.backtest as backtest_mod

    assert backtest_mod.paper_pipeline is paper_pipeline
    assert backtest_mod.prop_processing is prop_processing
    assert backtest_mod.run_pipeline is run_pipeline


# ---------------------------------------------------------------------------
# 2. Extracted run_pipeline gates behave identically through the facade
# ---------------------------------------------------------------------------


def _loop(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_spring_training_gate_blocks_mlb_preseason_range(tmp_path):
    async def _case():
        from unittest.mock import MagicMock
        from tools.schema.engine import ensure_schema
        import aiosqlite

        db_path = str(tmp_path / "spring.db")
        await ensure_schema(db_path)
        db = await aiosqlite.connect(db_path)

        engine = BacktestEngine.__new__(BacktestEngine)
        engine._db = db
        engine.db = None  # no game_results source → March-20 fallback applies
        engine.db_path = db_path
        hm = MagicMock()
        hm.get_hypothesis = _get_mlb
        engine.hypothesis_manager = hm
        out = await engine.run_backtest("hyp-mlb", "2026-02-10", "2026-03-05")
        await db.close()
        return out

    out = _loop(_case())
    assert out["error"] == "spring_training"
    assert out["total_events"] == 0
    assert out["signals_generated"] == 0


async def _get_mlb(hid):
    return {
        "status": "backtesting",
        # side_filter present so we reach the spring-training gate first
        "model_config": {"side_filter": "Over"},
        "edge_threshold": 0.02,
        "market_type": "totals",
        "thesis": "",
        "name": "mlb_totals_over",
        "sport": "baseball_mlb",
    }


def test_side_filter_gate_rejects_binary_both_sides(tmp_path, monkeypatch):
    monkeypatch.delenv("CALLISTO_ALLOW_BOTH_SIDES", raising=False)

    async def _case():
        from unittest.mock import MagicMock
        from tools.schema.engine import ensure_schema
        import aiosqlite

        db_path = str(tmp_path / "gate.db")
        await ensure_schema(db_path)
        db = await aiosqlite.connect(db_path)

        engine = BacktestEngine.__new__(BacktestEngine)
        engine._db = db
        engine.db = db
        engine.db_path = db_path
        engine._run_fingerprints = {}
        engine._RUN_FP_MAX = 500
        hm = MagicMock()

        async def _get(hid):
            return {
                "status": "backtesting",
                "model_config": {},
                "edge_threshold": 0.02,
                "market_type": "totals",
                "thesis": "",
                "name": "generic_totals_no_side",
                "sport": "basketball_nba",
            }

        hm.get_hypothesis = _get
        engine.hypothesis_manager = hm
        out = await engine.run_backtest("hyp-totals", "2026-01-01", "2026-01-05")
        await db.close()
        return out

    out = _loop(_case())
    assert out["error"] == "side_filter_required"
    assert "side_filter" in out["detail"]
    assert out["total_events"] == 0


def test_date_range_safety_caps_future_end_dates():
    """end_date today/future → capped to yesterday → empty range error for
    an impossible start>end, or proceeds — but never returns future data."""
    engine = _engine_with_hypothesis("backtesting")
    from datetime import datetime, timedelta

    yesterday = str(datetime.utcnow().date() - timedelta(days=40))
    today = str(datetime.utcnow().date() + timedelta(days=3))
    # start after capped end → "No valid date range"
    out = _loop(engine.run_backtest("hyp-x", "2099-01-01", today))
    assert out.get("error") == "No valid date range"


# ---------------------------------------------------------------------------
# 3. Real moved helper end-to-end: process_prop_snapshots
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmpdb(tmp_path):
    import aiosqlite
    from tools.schema.engine import ensure_schema

    db_path = str(tmp_path / "slice4.db")

    async def _make():
        await ensure_schema(db_path)
        return await aiosqlite.connect(db_path)

    conn = asyncio.new_event_loop().run_until_complete(_make())
    yield conn
    conn.close()


BOOKS_OVER_UNDER = [
    # Consensus around fair ~0.55 Over among non-target books…
    {"book": "pinnacle", "side": "Over", "price_american": -120},
    {"book": "pinnacle", "side": "Under", "price_american": 100},
    {"book": "fanduel", "side": "Over", "price_american": -118},
    {"book": "fanduel", "side": "Under", "price_american": -102},
    {"book": "mgm", "side": "Over", "price_american": -122},
    {"book": "mgm", "side": "Under", "price_american": 102},
    # …but draftkings (the target) is way off on the Over.
    {"book": "draftkings", "side": "Over", "price_american": 105},
    {"book": "draftkings", "side": "Under", "price_american": -125},
]

PROP_LINE = {
    "player": "LeBron James",
    "market": "player_points",
    "line": 25.5,
    "event_id": "evt-1",
    "game_date": "2026-08-20",
    "books": BOOKS_OVER_UNDER,
}


def test_process_prop_snapshots_generates_signal_and_rows(tmpdb):
    _loop(_prop_snapshots_case(tmpdb))


async def _prop_snapshots_case(db):
    events, signals = await prop_processing.process_prop_snapshots(
        db,
        run_id="run-p1",
        hypothesis_id="hyp-prop",
        prop_lines=[PROP_LINE],
        target_book="draftkings",
        edge_threshold=0.02,
        devig_method="multiplicative",
        config={},
        h_sport="basketball_nba",
        filters=None,
    )
    assert events == 2  # Over + Under both evaluated
    # DK Over (+105) vs ~0.55 fair → big positive edge → signal.
    # DK Under (-125) vs ~0.45 fair → negative edge → NOT a signal.
    assert signals == 1

    rows = await db.execute_fetchall(
        "SELECT run_id, player, market, line, side, book, edge, "
        "signal_generated FROM backtest_events WHERE run_id='run-p1'"
    )
    assert len(rows) == 2
    sides = {r[4] for r in rows}
    assert sides == {"Over", "Under"}
    for r in rows:
        assert r[0] == "run-p1"
        assert r[1] == "LeBron James"
        assert r[5] == "draftkings"
    # Exactly the Over row is flagged as a signal
    sig_rows = [r for r in rows if r[7] == 1]
    assert len(sig_rows) == 1
    assert sig_rows[0][4] == "Over"
    for r in rows:
        # Edge magnitude clipped at MAX_EDGE_MAGNITUDE
        assert abs(r[6]) <= prop_processing.MAX_EDGE_MAGNITUDE + 1e-12


def test_clip_edge_clamps_both_directions():
    clip = prop_processing.clip_edge
    assert clip(0.30) == prop_processing.MAX_EDGE_MAGNITUDE
    assert clip(-0.30) == -prop_processing.MAX_EDGE_MAGNITUDE
    assert clip(0.05) == 0.05
    assert clip(-0.05) == -0.05
    assert clip(0.15) == 0.15  # exactly at cap unchanged


def test_process_prop_snapshots_respects_side_filter(tmpdb):
    _loop(_prop_side_filter_case(tmpdb))


async def _prop_side_filter_case(db):
    events, signals = await prop_processing.process_prop_snapshots(
        db,
        run_id="run-p2",
        hypothesis_id="hyp-prop",
        prop_lines=[PROP_LINE],
        target_book="draftkings",
        edge_threshold=0.02,
        devig_method="multiplicative",
        config={},
        h_sport="basketball_nba",
        filters={"side_filter": "Over"},
    )
    assert events == 1 and signals == 1
    rows = await db.execute_fetchall(
        "SELECT side FROM backtest_events WHERE run_id='run-p2'"
    )
    assert [r[0] for r in rows] == ["Over"]


def test_process_prop_snapshots_requires_both_sides(tmpdb):
    n = _loop(_prop_one_sided_case(tmpdb))
    assert n == (0, 0)


async def _prop_one_sided_case(db):
    one_sided = dict(PROP_LINE)
    one_sided["books"] = [b for b in BOOKS_OVER_UNDER if b["side"] == "Over"]
    one_sided["event_id"] = "evt-2"
    return await prop_processing.process_prop_snapshots(
        db,
        run_id="run-p3",
        hypothesis_id="hyp-prop",
        prop_lines=[one_sided],
        target_book="draftkings",
        edge_threshold=0.02,
        devig_method="multiplicative",
        config={},
        h_sport="basketball_nba",
        filters=None,
    )


def test_process_prop_snapshots_skips_when_target_book_absent(tmpdb):
    n = _loop(_prop_no_target_case(tmpdb))
    assert n == (0, 0)


async def _prop_no_target_case(db):
    books = [b for b in BOOKS_OVER_UNDER if b["book"] != "draftkings"]
    line = dict(PROP_LINE)
    line["books"] = books
    line["event_id"] = "evt-3"
    return await prop_processing.process_prop_snapshots(
        db,
        run_id="run-p4",
        hypothesis_id="hyp-prop",
        prop_lines=[line],
        target_book="bet365",
        edge_threshold=0.02,
        devig_method="multiplicative",
        config={},
        h_sport="basketball_nba",
        filters=None,
    )


# ---------------------------------------------------------------------------
# 4. Real moved helper: inline game props path (facade delegation)
# ---------------------------------------------------------------------------


def test_process_game_props_delegates_and_writes_rows(tmpdb):
    _loop(_inline_props_case(tmpdb))


async def _inline_props_case(db):
    engine = BacktestEngine.__new__(BacktestEngine)
    engine._db = db

    game = {
        "id": "gid-9",
        "sport_key": "basketball_nba",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [
            {
                "key": book,
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"name": "Over", "description": "LeBron James",
                             "point": 25.5, "price": price_over},
                            {"name": "Under", "description": "LeBron James",
                             "point": 25.5, "price": price_under},
                        ],
                    }
                ],
            }
            for book, price_over, price_under in [
                ("pinnacle", -120, 100),
                ("fanduel", -118, -102),
                ("mgm", -122, 102),
                ("bet365", -120, 100),
                ("draftkings", 105, -125),
            ]
        ],
    }

    events, signals = await engine._process_game_props(
        run_id="run-i1",
        hypothesis_id="hyp-prop",
        game=game,
        game_date="2026-08-20",
        snapshot_time="2026-08-20T12:00:00+00:00",
        market_type="player_points",
        target_book="draftkings",
        edge_threshold=0.02,
        devig_method="multiplicative",
        min_books=2,
        config={},
        filters=None,
    )
    assert events == 2
    # 4 non-target books clears MIN_BOOKS_FOR_SIGNAL; DK Over at +105 vs
    # ~0.55 consensus fair is a clear positive edge → Over is flagged.
    assert signals >= 1

    rows = await db.execute_fetchall(
        "SELECT player, market, side, book, model_factors, kelly_fraction "
        "FROM backtest_events WHERE run_id='run-i1'"
    )
    assert len(rows) == 2
    factors = json.loads(rows[0][4])
    assert factors["edge_method"] in ("cross_book_best_line", "consensus_devig")
    assert "pinnacle" in factors["contributing_books"]
    # Kelly fractions are stored rounded to 6dp at row-build time only.
    for r in rows:
        if r[5] is not None:
            assert r[5] == round(r[5], 6)


def test_inline_props_min_books_gate_blocks_thin_markets(tmpdb):
    _loop(_thin_market_case(tmpdb))


async def _thin_market_case(db):
    engine = BacktestEngine.__new__(BacktestEngine)
    engine._db = db

    game = {
        "id": "gid-thin",
        "sport_key": "basketball_nba",
        "bookmakers": [
            {
                "key": "pinnacle",
                "markets": [{
                    "key": "player_points",
                    "outcomes": [
                        {"name": "Over", "description": "Player X",
                         "point": 10.5, "price": -110},
                        {"name": "Under", "description": "Player X",
                         "point": 10.5, "price": -110},
                    ],
                }],
            }
        ],
    }
    events, signals = await engine._process_game_props(
        run_id="run-thin",
        hypothesis_id="hyp-prop",
        game=game,
        game_date="2026-08-20",
        snapshot_time="now",
        market_type="player_points",
        target_book="pinnacle",
        edge_threshold=0.01,
        devig_method="multiplicative",
        min_books=2,
        config={},
        filters=None,
    )
    assert events == 0 and signals == 0
