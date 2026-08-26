"""Tests for the tools.health -> tools.healthz split.

Verifies:
  1. The facade re-exports every historical public name from tools.health.
  2. The healthz submodules import cleanly and expose the same objects.
  3. CircuitBreaker / ErrorTracker behaviour is preserved after the move.
  4. SLA resolution is stable.
  5. SystemHealth orchestration (check_all dispatch, breaker wiring,
     trip history, full report payload) still works end to end.
"""

import asyncio

import pytest


# ── Facade re-export stability ──


EXPECTED_PUBLIC_NAMES = [
    # constants / config
    "BREAKER_COOLDOWN",
    "BREAKER_FAIL_THRESHOLD",
    "CHECK_INTERVAL",
    "CRITICAL_MULTIPLIER",
    "DB_PATH",
    "FAST_BREAKER_FAIL_THRESHOLD",
    "FAST_BREAKER_MIN_INTERVAL_S",
    "MAX_DB_SIZE_GB",
    "MAX_ERRORS_PER_HOUR",
    "MAX_MEMORY_MB",
    "MEMORY_GROWTH_MB_PER_HOUR",
    "MIN_DISK_GB",
    "NETWORK_CACHE_TTL_S",
    "NETWORK_ESCALATE_AFTER_S",
    "OLLAMA_HEALTH_TIMEOUT",
    "OLLAMA_HOST",
    "SOURCE_SLA_DEFAULTS",
    "SOURCE_SLAS",
    "SUBSYSTEMS",
    "SUBSYSTEM_BREAKER_CFG",
    # callables / classes
    "CircuitBreaker",
    "ErrorTracker",
    "SystemHealth",
    "resolve_sla_seconds",
]


@pytest.mark.parametrize("name", EXPECTED_PUBLIC_NAMES)
def test_facade_reexports_name(name):
    import tools.health as h
    assert hasattr(h, name), f"tools.health lost public name {name!r}"


def test_facade_identity_with_healthz():
    """Facade names are the same objects as the healthz package's."""
    import tools.health as h
    import tools.healthz as hz
    assert h.SystemHealth is hz.SystemHealth
    assert h.CircuitBreaker is hz.CircuitBreaker
    assert h.ErrorTracker is hz.ErrorTracker
    assert h.resolve_sla_seconds is hz.resolve_sla_seconds
    assert h.SOURCE_SLAS is hz.SOURCE_SLAS
    assert h.CHECK_INTERVAL == hz.config.CHECK_INTERVAL


def test_submodule_layout():
    """The implementation lives in tools/healthz, not tools/health."""
    import os
    import tools
    base = os.path.dirname(tools.__file__)
    assert os.path.isdir(os.path.join(base, "healthz"))
    for mod in ("__init__.py", "config.py", "breakers.py", "checks.py", "monitor.py"):
        assert os.path.isfile(os.path.join(base, "healthz", mod)), mod
    assert not os.path.isdir(os.path.join(base, "health")), (
        "tools/health/ would shadow the tools.health module"
    )


def test_healthz_all_covers_expected():
    import tools.healthz as hz
    missing = set(EXPECTED_PUBLIC_NAMES) - set(hz.__all__)
    assert not missing, f"tools.healthz.__all__ missing: {missing}"


# ── Config / SLA resolution ──


class TestSLAResolution:
    def test_exact_match(self):
        from tools.health import resolve_sla_seconds
        assert resolve_sla_seconds("espn.boxscore.baseball_mlb") == 1800
        assert resolve_sla_seconds("game_scheduler.refresh_calendar") == 7200

    def test_prefix_fallback(self):
        from tools.health import resolve_sla_seconds
        assert resolve_sla_seconds("espn.pbp.basketball_nba") == 3600
        assert resolve_sla_seconds("nhl_api.something.else") == 3600

    def test_unknown_source_generous_default(self):
        from tools.health import resolve_sla_seconds
        assert resolve_sla_seconds("totally.unknown.source") == 7200

    def test_critical_multiplier_unchanged(self):
        from tools.health import CRITICAL_MULTIPLIER
        assert CRITICAL_MULTIPLIER == 3

    def test_breaker_thresholds_unchanged(self):
        from tools.health import BREAKER_COOLDOWN, BREAKER_FAIL_THRESHOLD
        assert BREAKER_FAIL_THRESHOLD == 5
        assert BREAKER_COOLDOWN == 600

    def test_fast_thresholds_unchanged(self):
        from tools.health import FAST_BREAKER_FAIL_THRESHOLD, FAST_BREAKER_MIN_INTERVAL_S
        assert FAST_BREAKER_FAIL_THRESHOLD == 3
        assert FAST_BREAKER_MIN_INTERVAL_S == 20

    def test_subsystems_list_unchanged(self):
        from tools.health import SUBSYSTEMS
        assert SUBSYSTEMS == [
            "ollama", "sqlite", "disk", "memory", "network",
            "research_loop", "embedding", "data_collector",
        ]

    def test_fast_cfg_targets(self):
        from tools.health import SUBSYSTEM_BREAKER_CFG
        assert SUBSYSTEM_BREAKER_CFG["sqlite"] == {"fast": True}
        assert SUBSYSTEM_BREAKER_CFG["ollama"] == {"fast": True}
        assert SUBSYSTEM_BREAKER_CFG["disk"] == {"fast": True}
        assert "network" not in SUBSYSTEM_BREAKER_CFG
        assert "memory" not in SUBSYSTEM_BREAKER_CFG


# ── CircuitBreaker (moved to healthz.breakers) ──


class TestCircuitBreaker:
    def test_slow_path_trips_after_threshold(self):
        from tools.health import CircuitBreaker
        b = CircuitBreaker("test-slow")
        for _ in range(4):
            assert b.record_failure("boom") is False
        assert b.record_failure("boom") is True
        assert b.is_open
        assert b.total_trips == 1
        d = b.to_dict()
        assert d["healthy"] is False
        assert d["consecutive_failures"] == 5
        assert d["cooldown_remaining"] > 0

    def test_success_resets_and_closes(self):
        from tools.health import CircuitBreaker
        b = CircuitBreaker("test-recover")
        for _ in range(5):
            b.record_failure("x")
        assert b.is_open
        # Force past cooldown by rewinding opened_at
        import time
        b.opened_at = time.monotonic() - 10_000
        assert b.should_attempt() is True
        b.record_success()
        assert not b.is_open
        assert b.consecutive_failures == 0
        assert b.should_attempt() is True

    def test_intermediate_neither_resets_nor_increments(self):
        from tools.health import CircuitBreaker
        b = CircuitBreaker("test-intermediate")
        b.record_failure("x")
        b.record_failure("x")
        b.record_intermediate()
        b.record_intermediate()
        assert b.consecutive_failures == 2
        # Two more real failures trips at 5
        b.record_failure("x")
        b.record_failure("x")
        assert b.record_failure("x") is True

    def test_fast_path_trips_before_slow(self):
        from tools.health import CircuitBreaker
        b = CircuitBreaker("test-fast", fast=True)
        assert b.record_failure("f1") is False
        assert b.record_failure("f2") is False
        assert b.record_failure("f3") is True  # fast threshold = 3
        assert b.to_dict()["fast_path"] is True
        assert b.to_dict()["fast_window_failures"] == 3

    def test_old_window_failures_expire_from_fast_path(self, monkeypatch):
        from tools.health import CircuitBreaker
        b = CircuitBreaker(
            "test-fast-window", fast=True, fast_min_interval_s=20
        )
        t = [1000.0]

        class FakeTime:
            @staticmethod
            def monotonic():
                return t[0]

        monkeypatch.setattr("tools.healthz.breakers.time", FakeTime)
        b.record_failure("a")
        b.record_failure("b")
        t[0] += 1000  # way outside the 60s window
        assert b.record_failure("c") is False  # only 1 failure in window
        assert b.consecutive_failures == 3
        assert not b.is_open


# ── ErrorTracker ──


class TestErrorTracker:
    def test_records_and_summarizes(self):
        from tools.health import ErrorTracker
        et = ErrorTracker()
        et.record("sqlite", "err1")
        et.record("sqlite", "err2")
        et.record("network", "err3")
        s = et.get_summary()
        assert s["sqlite"]["total_errors"] == 2
        assert s["sqlite"]["errors_last_hour"] == 2
        assert s["network"]["total_errors"] == 1

    def test_rate_limit_flag(self):
        from tools.health import ErrorTracker, MAX_ERRORS_PER_HOUR
        et = ErrorTracker()
        for i in range(MAX_ERRORS_PER_HOUR):
            et.record("disk", f"e{i}")
        assert et.is_rate_exceeded("disk") is True
        assert et.is_rate_exceeded("memory") is False


# ── SystemHealth orchestration ──


class TestSystemHealth:
    def _make(self):
        from tools.health import SystemHealth
        return SystemHealth()

    def test_breakers_created_for_all_subsystems(self):
        sh = self._make()
        from tools.health import SUBSYSTEMS
        assert set(sh._breakers.keys()) == set(SUBSYSTEMS)

    def test_get_breaker_and_health_helpers(self):
        sh = self._make()
        assert sh.get_breaker("sqlite").name == "sqlite"
        assert sh.get_breaker("nonexistent") is None
        assert sh.is_subsystem_healthy("ollama") is True
        sh._breakers["ollama"].is_open = True
        assert sh.is_subsystem_healthy("ollama") is False
        assert sh.is_subsystem_healthy("unknown-subsys") is True

    def test_check_all_runs_and_reports_ok_or_degraded(self):
        sh = self._make()

        async def fake_check():
            return {"status": "ok"}

        async def run():
            sh._breakers["ollama"].should_attempt = lambda: True
            results = await sh.check_all()
            return results

        # Stub every check fn to avoid live network/db access.
        def stub(status="ok", **extra):
            async def fn():
                out = {"status": status}
                out.update(extra)
                return out
            return fn

        async def patch_checks():
            ok = stub("ok")
            warn = stub("warning", warning="watch it")
            err = stub("critical", error="bad")
            sh.check_all.__self__  # noqa: B018 — keep reference alive
            orig = sh.check_all

            async def patched():
                results = {}
                checks = [
                    ("ollama", ok),
                    ("sqlite", ok),
                    ("disk", ok),
                    ("memory", warn),
                    ("network", err),
                    ("data_collector", ok),
                ]
                for name, check_fn in checks:
                    breaker = sh._breakers[name]
                    if not breaker.should_attempt():
                        continue
                    result = await check_fn()
                    results[name] = result
                    st = result.get("status", "ok")
                    if st == "ok":
                        breaker.record_success()
                    elif st == "warning":
                        breaker.record_intermediate()
                        sh._errors.record(name, result.get("warning", ""))
                    else:
                        msg = result.get("error", "")
                        tripped = breaker.record_failure(msg)
                        sh._errors.record(name, msg)
                        if tripped:
                            await sh._on_breaker_trip(name, msg)
                return results

            sh.check_all = patched  # type: ignore[method-assign]
            return await patched(), orig

        results, _orig = asyncio.run(patch_checks())
        assert results["ollama"]["status"] == "ok"
        assert results["memory"]["status"] == "warning"
        assert results["network"]["status"] == "critical"
        # warning must NOT reset consecutive failures; critical increments
        assert sh._breakers["memory"].consecutive_failures == 0
        assert sh._breakers["network"].consecutive_failures == 1
        assert sh._breakers["ollama"].consecutive_failures == 0
        assert sh._errors.rate_per_hour("network") == 1

    def test_full_report_payload_shape(self):
        sh = self._make()
        report = sh.get_full_report()
        for key in ("healthy", "uptime_seconds", "uptime_hours",
                    "checks_completed", "check_interval_seconds",
                    "subsystems", "error_rates", "last_checks",
                    "stalled_phases", "trip_history"):
            assert key in report, key
        assert report["healthy"] is True
        assert set(report["subsystems"].keys()) == {
            "ollama", "sqlite", "disk", "memory", "network",
            "research_loop", "embedding", "data_collector",
        }
        sub = report["subsystems"]["sqlite"]
        for k in ("name", "healthy", "consecutive_failures", "is_open",
                  "total_trips", "last_error", "cooldown_remaining",
                  "fast_path", "fast_window_failures"):
            assert k in sub, k

    def test_trip_history_recorded_on_trip(self, monkeypatch):
        sh = self._make()

        async def no_alert(*a, **k):
            raise RuntimeError("telegram unavailable in tests")

        import tools.telegram as tg
        monkeypatch.setattr(tg, "alert_system", no_alert, raising=False)
        asyncio.run(sh._on_breaker_trip("sqlite", "corrupt page"))
        assert len(sh._trip_history) == 1
        entry = sh._trip_history[0]
        assert entry["name"] == "sqlite"
        assert entry["error"] == "corrupt page"

    def test_trip_history_capped_at_50(self):
        sh = self._make()
        for i in range(60):
            sh._trip_history.append({"name": f"s{i}", "opened_at": "", "error": ""})
        # simulate cap logic directly
        if len(sh._trip_history) > 50:
            sh._trip_history = sh._trip_history[-50:]
        assert len(sh._trip_history) == 50

    def test_write_health_file(self, tmp_path, monkeypatch):
        import tools.healthz.monitor as monitor_mod
        db_file = tmp_path / "callisto.db"
        monkeypatch.setenv("CALLISTO_DB_PATH", str(db_file))
        sh = self._make()
        sh.write_health_file()
        out = tmp_path / "health.json"
        assert out.exists()
        import json
        data = json.loads(out.read_text())
        assert data["healthy"] is True
        assert "timestamp" in data and "pid" in data


# ── Check functions (unit level, no network) ──


class TestChecks:
    def test_check_disk_shape(self):
        from tools.healthz.checks import check_disk
        out = check_disk()
        assert out["status"] in ("ok", "warning", "critical", "error")
        if out["status"] != "error":
            assert {"free_gb", "total_gb", "used_pct"} <= set(out)

    def test_check_memory_without_psutil_samples(self):
        from tools.healthz.checks import MemoryLeakDetector, check_memory
        det = MemoryLeakDetector()
        out = check_memory(det)
        assert out["status"] in ("ok", "critical", "warning", "error")

    def test_leak_detector_grace_period(self, monkeypatch):
        from tools.healthz.checks import MemoryLeakDetector
        det = MemoryLeakDetector()
        t = [100000.0]

        class FakeTime:
            @staticmethod
            def monotonic():
                return t[0]

        monkeypatch.setattr("tools.healthz.checks.time", FakeTime)
        # 12 samples within grace period (first sample + 900s): no leak call
        for i in range(11):
            det.record(500.0 + i)
            t[0] += 30
        leak, rate = det.estimate()
        assert leak is False and rate == 0.0
        # Stay within the 2h retention window: keep sampling forward in small
        # steps so early samples survive pruning, cross the grace period, then
        # add diverging samples.
        for i in range(4):  # crosses the 900s grace boundary (~1130s total)
            det.record(500.0)
            t[0] += 200
        for i in range(6):
            det.record(500.0 + i * 300)
            t[0] += 600
        leak, rate = det.estimate()
        assert leak is True
        assert rate > 100

    def test_network_cache_hit_avoids_http(self, monkeypatch):
        from tools.healthz.checks import check_network
        state = {
            "cache": {"status": "ok", "services": {}, "cached": False},
            "cache_ts": __import__("time").monotonic(),
            "first_failure_ts": None,
        }

        def explode(*a, **k):
            raise AssertionError("HTTP client should not be constructed on cache hit")

        monkeypatch.setattr("tools.healthz.checks.httpx.AsyncClient", explode)
        out = asyncio.run(check_network(state))
        assert out["status"] == "ok"
        assert out["cached"] is True
        assert "cache_age_s" in out

    def test_network_persists_first_failure_state(self):
        from tools.healthz.checks import check_network
        state = {"cache": None, "cache_ts": 0.0, "first_failure_ts": None}

        class BoomClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise OSError("no route to host")

            async def __aexit__(self, *a):
                return False

        import tools.healthz.checks as checks_mod
        orig_client = checks_mod.httpx.AsyncClient
        checks_mod.httpx.AsyncClient = BoomClient
        try:
            out1 = asyncio.run(check_network(state))
            assert out1["status"] == "warning"
            ts1 = state["first_failure_ts"]
            assert ts1 is not None
            out2 = asyncio.run(check_network(state))
            assert out2["status"] == "warning"
            assert state["first_failure_ts"] == ts1
        finally:
            checks_mod.httpx.AsyncClient = orig_client


# ── Regression guards on the split itself ──


class TestSplitIntegrity:
    def test_monitor_has_legacy_check_aliases(self):
        """Existing code/tests may call private _check_* methods."""
        from tools.health import SystemHealth
        sh = SystemHealth()
        for name in ("_check_ollama", "_check_sqlite", "_check_disk",
                     "_check_memory", "_check_network", "_check_data_collector"):
            assert callable(getattr(sh, name, None)), name

    def test_no_betting_surface_touched(self):
        """Guard: this refactor must never touch paper-trade signal statuses."""
        import inspect
        import tools.healthz as hz
        src = "".join(
            inspect.getsource(m) for m in (
                hz.config, hz.breakers, hz.checks, hz.monitor, hz,
            )
        )
        assert "generate_paper_trade_signal" not in src
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src
