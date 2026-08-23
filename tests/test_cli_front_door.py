"""Tests for the callisto front-door CLI (callisto.py).

The CLI is how a human drives one question and reads system state. These
tests pin its contract: honest exit codes, graceful handling of a machine
without the lifecycle database, doctor that diagnoses instead of crashing,
and ask that reports the pipeline's verdict without inventing one.
"""
import asyncio
import json
import sqlite3

import pytest

from callisto import _cmd_ask, _cmd_doctor, _cmd_status, build_parser


# ── parser ────────────────────────────────────────────────────────────────

class TestParser:
    def test_ask_requires_a_question(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["ask"])

    def test_ask_defaults(self):
        args = build_parser().parse_args(["ask", "why is the sky blue"])
        assert args.question == "why is the sky blue"
        assert args.backend is None
        assert args.self_review is False

    def test_backend_flag_routes(self):
        args = build_parser().parse_args(
            ["ask", "--backend", "gpu1", "q"])
        assert args.backend == "gpu1"

    def test_unknown_command_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["frobnicate"])


# ── status ────────────────────────────────────────────────────────────────

def _make_db(path, with_lifecycle=True):
    conn = sqlite3.connect(path)
    if with_lifecycle:
        conn.executescript("""
            CREATE TABLE hypotheses (
                hypothesis_id TEXT PRIMARY KEY, name TEXT, sport TEXT,
                market_type TEXT, status TEXT, rejection_reason TEXT,
                updated_at TEXT);
            CREATE TABLE backtest_events (
                id INTEGER PRIMARY KEY, hypothesis_id TEXT,
                signal_generated INTEGER, edge REAL, game_date TEXT);
        """)
        conn.execute("INSERT INTO hypotheses VALUES"
                     " ('h1','Test edge','nba','h2h','backtesting',NULL,"
                     "  '2026-08-23')")
        conn.execute("INSERT INTO backtest_events VALUES"
                     " (1,'h1',1,0.03,'2026-01-05'),"
                     " (2,'h1',0,-0.01,'2026-01-06')")
        conn.execute("INSERT INTO hypotheses VALUES"
                     " ('h2','Bad idea','nfl','ml','rejected','p too high',"
                     "  '2026-08-22')")
    else:
        # e.g. a laptop checkout carrying only hermes memory tables
        conn.execute("CREATE TABLE hermes_learnings (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


class TestStatus:
    def test_missing_db_is_exit_zero_with_message(self, capsys):
        args = build_parser().parse_args(
            ["status", "--providers", "x"])
        args.__dict__["db_path"] = None  # not used; env drives it
        rc = _run_status(db="/nonexistent/dir/x.db")
        assert rc == 0

    def test_db_without_lifecycle_tables_is_graceful(self, tmp_path, capsys):
        db = str(tmp_path / "mem.db")
        _make_db(db, with_lifecycle=False)
        rc = _run_status(db=db)
        out = capsys.readouterr().out
        assert rc == 0
        assert "no hypotheses table" in out.lower() or \
               "has not run" in out.lower()

    def test_full_db_reports_lifecycle_and_signal_rate(self, tmp_path, capsys):
        db = str(tmp_path / "full.db")
        _make_db(db)
        rc = _run_status(db=db)
        out = capsys.readouterr().out
        assert rc == 0
        assert "HYPOTHESIS LIFECYCLE" in out
        assert "backtesting" in out
        assert "1/2" or "50.0" in out          # signal rate visible either way
        assert "Bad idea" in out               # recent rejections listed


def _run_status(db: str) -> int:
    """Run _cmd_status against an explicit DB path via the env var."""
    import os
    old = os.environ.get("CALLISTO_DB_PATH")
    os.environ["CALLISTO_DB_PATH"] = db
    try:
        args = build_parser().parse_args(["status"])
        return _cmd_status(args)
    finally:
        if old is None:
            os.environ.pop("CALLISTO_DB_PATH", None)
        else:
            os.environ["CALLISTO_DB_PATH"] = old


# ── doctor ────────────────────────────────────────────────────────────────

class TestDoctor:
    def test_unreadable_config_diagnosed_not_crashed(self, tmp_path, capsys):
        """Regression: provs was referenced after a failed config load and
        raised UnboundLocalError — a diagnostic tool must never be the crash."""
        rc = _run_doctor(providers=str(tmp_path / "missing.yaml"))
        out = capsys.readouterr().out + capsys.readouterr().err
        assert rc == 1
        assert "config unreadable" in out
        assert "PROBLEMS FOUND" in out

    def test_real_config_reports_ok(self, capsys):
        from callisto import _default_providers_path
        rc = _run_doctor(providers=_default_providers_path())
        out = capsys.readouterr().out
        assert "source registry" in out
        assert "adapters registered" in out
        # this machine is configured and healthy
        assert rc == 0
        assert "doctor: OK" in out


def _run_doctor(providers: str) -> int:
    args = build_parser().parse_args(["doctor", "--providers", providers])
    return _cmd_doctor(args)


# ── ask ───────────────────────────────────────────────────────────────────

class FakeRouter:
    """Minimal router standing in for ProviderRouter at the seam."""
    class _Ledger:
        def snapshot(self):
            return {"by_tier": {"gpu1": {"calls": 3}}}
    def __init__(self, endpoints=("gpu1",), health=None):
        self.endpoints = list(endpoints)
        self.task_classes = {"decompose": "gpu1"}
        self.default_tier_name = "gpu1"
        self._health = health or {"status": "ok"}
        self.cost_ledger = self._Ledger()
        self.check_health_called_with = None
    async def check_health(self, tier):
        self.check_health_called_with = tier
        return self._health


class FakeEngine:
    def __init__(self, *, model=None, adversary_router=None):
        self.model = model
        self.adversary_router = adversary_router
        self.ran_with = None
    async def run(self, question):
        self.ran_with = question
        from types import SimpleNamespace as NS
        leaf = NS(text="sub-question", answer="an answer",
                  tier="SPECULATIVE", confidence=0.34)
        fetch = NS(source_name="openalex")
        ob = NS(text="only one independent source")
        return NS(sealed=True, refusal_reason="", leaves=[leaf],
                  confidence_score=0.34, confidence_tier="SPECULATIVE",
                  conclusion="sealed conclusion", fetches=[fetch],
                  objections=[ob], notes=[], artifact_refs=[])


class TestAsk:
    @pytest.fixture
    def wired(self, monkeypatch):
        router = FakeRouter()
        engines = []
        def load_router(path): return router
        def make_engine(router_, self_review):
            eng = FakeEngine(
                model=router_,
                adversary_router=(None if self_review else router_))
            engines.append(eng)
            return eng
        monkeypatch.setattr("callisto._load_router", load_router)
        monkeypatch.setattr("callisto._make_engine", make_engine)
        return router, engines

    def _args(self, q="test question"):
        return build_parser().parse_args(["ask", q])

    def test_sealed_result_prints_verdict_and_exits_zero(self, wired, capsys):
        router, engines = wired
        rc = asyncio.run(_cmd_ask(self._args()))
        out = capsys.readouterr().out
        assert rc == 0
        assert engines[0].ran_with == "test question"
        assert "SEALED" in out and "0.34" in out
        assert "openalex" in out                      # sources named
        assert "one independent source" in out        # objection shown
        assert engines[0].adversary_router is router  # adversary wired

    def test_self_review_passes_no_adversary_router(self, wired, capsys):
        _, engines = wired
        args = build_parser().parse_args(["ask", "--self-review", "q"])
        asyncio.run(_cmd_ask(args))
        assert engines[0].adversary_router is None

    def test_unknown_backend_refuses_before_any_model_call(self, wired, capsys):
        router, engines = wired
        args = build_parser().parse_args(
            ["ask", "--backend", "nope", "q"])
        rc = asyncio.run(_cmd_ask(args))
        assert rc == 2
        assert "unknown provider tier 'nope'" in capsys.readouterr().out
        assert engines == []                          # engine never built

    def test_unreachable_provider_directs_to_doctor(self, wired, capsys):
        router, engines = wired
        async def boom(tier): raise ConnectionError("refused")
        router.check_health = boom
        rc = asyncio.run(_cmd_ask(self._args()))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unreachable" in out
        assert "doctor" in out                        # next step named
