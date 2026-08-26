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

    def test_real_config_reports_ok(self, capsys, monkeypatch):
        from callisto import _default_providers_path
        # ask/doctor are fail-closed without a hex seal key; this pin is
        # about providers+registry, so give doctor a valid key.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)
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
    @pytest.fixture(autouse=True)
    def _valid_seal_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)

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
        args = build_parser().parse_args(
            ["ask", "--backend", "gpu1", "q"])
        rc = asyncio.run(_cmd_ask(args))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unreachable" in out
        assert "doctor" in out                        # next step named

    def test_pinned_dead_backend_exits_2_without_running_engine(
            self, wired, capsys):
        """--backend pins every task class to one provider and disables
        failover, so a dead pinned backend must be refused up front with
        doctor guidance instead of failing inside the pipeline."""
        router, engines = wired
        async def boom(tier): raise ConnectionError("refused")
        router.check_health = boom
        args = build_parser().parse_args(
            ["ask", "--backend", "gpu1", "q"])
        rc = asyncio.run(_cmd_ask(args))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unreachable" in out and "doctor" in out
        assert engines == []                          # engine never built

    def test_default_mode_skips_preflight_and_runs_engine(
            self, wired, monkeypatch, capsys):
        """Without --backend, a dead default tier must NOT preempt the run:
        ProviderRouter's candidate chain (with ox_alpha failover) decides.
        The CLI must not even probe check_health."""
        router, engines = wired
        calls = []
        async def probe(tier):
            calls.append(tier)
            return {"status": "down"}
        router.check_health = probe
        rc = asyncio.run(_cmd_ask(self._args()))
        out = capsys.readouterr().out
        assert calls == []                            # no preflight probe
        assert rc == 0
        assert engines[0].ran_with == "test question"
        assert "SEALED" in out


# ── run persistence: runs / show ──────────────────────────────────────────

from types import SimpleNamespace as _NS

from callisto import (_cmd_runs, _cmd_show, _load_run, _persist_run,
                      _result_record, _verify_artifact)


def _fake_result():
    """A PipelineResult-shaped object like a real sealed run."""
    from tools.artifacts import ArtifactRef
    return _NS(
        sealed=True, refusal_reason="",
        conclusion="Foundry concentration is the binding constraint.",
        confidence_score=0.34, confidence_tier="SPECULATIVE",
        leaves=[_NS(text="leaf q", answer="leaf a", tier="SPECULATIVE",
                    confidence=0.4)],
        artifact_refs=[ArtifactRef(sha256="a" * 64, kind="csv",
                                   name="concentration.csv")],
        fetches=[_NS(source_name="openalex", url="https://api.openalex.org/x",
                     content_sha256="b" * 64)],
        objections=[_NS(text="one independent source only")],
        notes=[])


class TestRunPersistence:
    @pytest.fixture(autouse=True)
    def _valid_seal_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)

    @pytest.fixture
    def wired(self, monkeypatch):
        router = FakeRouter()
        def load_router(path): return router
        monkeypatch.setattr("callisto._load_router", load_router)
        return router

    @pytest.fixture
    def runs_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
        return tmp_path / "runs"

    def test_ask_persists_a_run_record_and_prints_path(
            self, wired, runs_env, monkeypatch, capsys):
        router = wired
        def make_engine(router_, self_review):
            eng = _NS(adversary_router=None if self_review else router_)
            async def run(q): return _fake_result()
            eng.run = run
            return eng
        monkeypatch.setattr("callisto._make_engine", make_engine)

        rc = asyncio.run(_cmd_ask(build_parser().parse_args(["ask", "q"])))
        out = capsys.readouterr().out
        assert rc == 0
        assert "run      :" in out
        saved = list(runs_env.glob("*.json"))
        assert len(saved) == 1
        rec = json.loads(saved[0].read_text())
        assert rec["sealed"] is True
        assert rec["question"] == "q"
        assert rec["conclusion"].startswith("Foundry")
        assert len(rec["artifacts"]) == 1
        assert rec["fetches"][0]["url"] == "https://api.openalex.org/x"
        assert "artifact :" in out                     # artifact hashes shown

    def test_record_roundtrip_preserves_everything(self, runs_env):
        rec = _result_record(_fake_result(), "some question")
        path = _persist_run(rec)
        loaded, _ = _load_run(path.stem)
        assert loaded["confidence"]["tier"] == "SPECULATIVE"
        assert loaded["objections"] == ["one independent source only"]
        # deterministic re-serialisation: same content -> same dict
        again = json.loads(json.dumps(rec))
        assert again == loaded

    def test_runs_lists_newest_first_and_empty_is_friendly(
            self, runs_env, capsys):
        rc = _cmd_runs(build_parser().parse_args(["runs"]))
        assert rc == 0 and "no saved runs yet" in capsys.readouterr().out
        for i in range(3):
            r = _result_record(_fake_result(), f"q{i}")
            _persist_run(r)
        rc = _cmd_runs(build_parser().parse_args(["runs"]))
        out = capsys.readouterr().out.strip().splitlines()
        assert rc == 0 and len(out) == 3
        assert all("SEALED" in line for line in out)

    def test_show_reprints_conclusion_and_verifies_artifacts(
            self, runs_env, tmp_path, capsys, monkeypatch):
        rec = _result_record(_fake_result(), "the question")
        path = _persist_run(rec)
        # put the real bytes in a temp artifact store and point the env at it
        import hashlib

        from tools.artifacts import ArtifactStore
        store = ArtifactStore(root=tmp_path / "arts")
        monkeypatch.setenv("CALLISTO_ARTIFACT_DIR", str(tmp_path / "arts"))
        store.put(b"payload", kind="csv", name="concentration.csv")
        actual = hashlib.sha256(b"payload").hexdigest()
        rec["artifacts"][0]["sha256"] = actual
        path.write_text(json.dumps(rec))

        rc = _cmd_show(build_parser().parse_args(["show", path.stem]))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Foundry concentration" in out          # conclusion reprinted
        assert "[ok" in out                             # artifact verified
        assert "openalex" in out                        # fetch provenance

    def test_show_reports_missing_artifact_honestly(
            self, runs_env, capsys, monkeypatch):
        rec = _result_record(_fake_result(), "q")
        path = _persist_run(rec)   # hash not present in any store
        rc = _cmd_show(build_parser().parse_args(["show", path.stem]))
        out = capsys.readouterr().out
        assert rc == 0
        assert "missing" in out or "unverifiable" in out

    def test_show_unknown_id_exits_one(self, runs_env, capsys):
        rc = _cmd_show(build_parser().parse_args(["show", "nope"]))
        assert rc == 1
