"""Tests for offloading blocking work off the FastAPI event loop.

Covers:
  * Source/contract: api.py uses asyncio.to_thread around simulate_portfolio,
    detect_regime, and write_health_file call sites.
  * _PORTFOLIO_SIM_CACHE is bounded (LRU, max 32 entries) with TTL.
  * /health write_health_file is debounced via a module-level timestamp.
  * The portfolio sim endpoint actually awaits simulate_portfolio on a
    worker thread (monkeypatched; no TestClient lifespan).

Follows tests/test_api_auth.py conventions: set CALLISTO_BIND_HOST before
importing api.py, and never enter the app lifespan.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sys
import threading
import time

import pytest


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:
    api_mod = None
    _import_err_msg = str(_import_err)
else:
    _import_err_msg = ""


# ---------------------------------------------------------------------------
# A1. Source/contract checks — to_thread near the right call sites.
# ---------------------------------------------------------------------------

def _api_source() -> str:
    with open(os.path.join(os.path.dirname(api_mod.__file__), "api.py")) as f:
        return f.read()


@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestSourceContract:
    def test_simulate_portfolio_offloaded(self):
        src = _api_source()
        assert "asyncio.to_thread(" in src
        # to_thread wrapping simulate_portfolio in the endpoint body — the
        # call site lives in tools/api/simulate.py since slice 5.
        from tools.api import simulate as _sim
        import inspect as _inspect

        sim_src = _inspect.getsource(_sim.simulate_portfolio_endpoint)
        assert "await asyncio.to_thread(\n        simulate_portfolio" in sim_src

    def test_detect_regime_offloaded(self):
        src = _api_source()
        assert "await asyncio.to_thread(detect_regime" in src

    def test_write_health_file_offloaded_and_debounced(self):
        src = _api_source()
        assert "await asyncio.to_thread(system_health.write_health_file)" in src
        assert "_HEALTH_FILE_DEBOUNCE_SECONDS" in src
        assert "_HEALTH_FILE_LAST_WRITE_TS" in src

    def test_all_live_sqlite_read_helper_is_async(self):
        assert hasattr(api_mod, "_fetch_live_hypothesis_ids")
        assert inspect.iscoroutinefunction(api_mod._fetch_live_hypothesis_ids)


# ---------------------------------------------------------------------------
# A2. Cache bound + TTL helpers (unit-tested without TestClient).
# ---------------------------------------------------------------------------

@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestPortfolioSimCache:
    def setup_method(self):
        api_mod._PORTFOLIO_SIM_CACHE.clear()

    def test_evicts_oldest_at_33rd_insert(self):
        for i in range(33):
            api_mod._store_portfolio_sim_cache(("k", i), (time.time(), {"i": i}))
            assert len(api_mod._PORTFOLIO_SIM_CACHE) <= 32
        assert len(api_mod._PORTFOLIO_SIM_CACHE) == 32
        # Oldest key ("k", 0) must be gone.
        assert api_mod._get_portfolio_sim_cache(("k", 0)) is None
        # Newest keys survive.
        assert api_mod._get_portfolio_sim_cache(("k", 32)) is not None
        assert api_mod._get_portfolio_sim_cache(("k", 1)) is not None

    def test_lru_refresh_prevents_eviction_of_hot_entry(self):
        for i in range(32):
            api_mod._store_portfolio_sim_cache(("k", i), (time.time(), {}))
        # Touch the oldest entry -> it becomes most-recently-used.
        assert api_mod._get_portfolio_sim_cache(("k", 0)) is not None
        api_mod._store_portfolio_sim_cache(("k", 99), (time.time(), {}))
        assert len(api_mod._PORTFOLIO_SIM_CACHE) <= 32
        # Hot entry survived; entry 1 (now oldest) was evicted instead.
        assert api_mod._get_portfolio_sim_cache(("k", 0)) is not None
        assert api_mod._get_portfolio_sim_cache(("k", 1)) is None

    def test_ttl_expiry(self):
        old_ttl = api_mod._PORTFOLIO_SIM_CACHE_TTL
        try:
            api_mod._store_portfolio_sim_cache(("expired",), (time.time() - old_ttl - 5, {}))
            assert api_mod._get_portfolio_sim_cache(("expired",)) is None
            assert ("expired",) not in api_mod._PORTFOLIO_SIM_CACHE
        finally:
            api_mod._PORTFOLIO_SIM_CACHE_TTL = old_ttl


# ---------------------------------------------------------------------------
# B. Endpoint actually awaits simulate_portfolio on a worker thread.
# Called directly (no TestClient) so lifespan is never entered.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(api_mod is None, reason=f"Could not import api module: {_import_err_msg}")
class TestEndpointOffload:
    def test_endpoint_awaits_on_worker_thread(self, monkeypatch):
        calls = {}

        class _FakeResult:
            def to_dict(self, include_paths=False):
                return {"ok": True}

        main_thread_id = threading.get_ident()

        def fake_simulate(**kwargs):
            calls["thread_id"] = threading.get_ident()
            time.sleep(0.05)
            return _FakeResult()

        monkeypatch.setattr(
            "tools.bankroll_sim.simulate_portfolio", fake_simulate, raising=False
        )
        api_mod._PORTFOLIO_SIM_CACHE.clear()

        resp = asyncio.run(
            api_mod.simulate_portfolio_endpoint(
                hypothesis_ids="a,b",
                n_sims=10,
                horizon_days=1,
                starting_bankroll=1000.0,
                kelly_fraction=0.25,
                all_live=False,
            )
        )
        assert resp.get("cached") is False and resp.get("ok") is True
        assert calls.get("thread_id") not in (None, main_thread_id)

        # Second identical call hits cache (no thread sleep re-run).
        t0 = time.time()
        resp2 = asyncio.run(
            api_mod.simulate_portfolio_endpoint(
                hypothesis_ids="b,a",
                n_sims=10,
                horizon_days=1,
                starting_bankroll=1000.0,
                kelly_fraction=0.25,
                all_live=False,
            )
        )
        assert resp2.get("cached") is True
        assert time.time() - t0 < 0.04

    def test_health_debounce_skips_recent_write(self, monkeypatch):
        writes = []

        class FakeSystemHealth:
            def write_health_file(self):
                writes.append(time.time())

        async def fake_build_report():
            return {"healthy": True}

        orig_ts = api_mod._HEALTH_FILE_LAST_WRITE_TS
        try:
            monkeypatch.setattr(api_mod, "system_health", FakeSystemHealth(), raising=False)
            monkeypatch.setattr(api_mod, "_build_health_report", fake_build_report, raising=False)

            api_mod._HEALTH_FILE_LAST_WRITE_TS = time.time() - 1.0  # recent
            report = asyncio.run(api_mod.health_check())
            assert report.get("healthy") is True
            assert writes == []  # debounced: skipped

            api_mod._HEALTH_FILE_LAST_WRITE_TS = time.time() - 60.0  # stale
            asyncio.run(api_mod.health_check())
            assert len(writes) == 1
            assert api_mod._HEALTH_FILE_LAST_WRITE_TS >= time.time() - 5.0
        finally:
            api_mod._HEALTH_FILE_LAST_WRITE_TS = orig_ts

    def test_health_never_fails_if_write_raises(self, monkeypatch):
        class BoomHealth:
            def write_health_file(self):
                raise OSError("disk full")

        async def fake_build_report():
            return {"healthy": True}

        orig_ts = api_mod._HEALTH_FILE_LAST_WRITE_TS
        try:
            monkeypatch.setattr(api_mod, "system_health", BoomHealth(), raising=False)
            monkeypatch.setattr(api_mod, "_build_health_report", fake_build_report, raising=False)
            api_mod._HEALTH_FILE_LAST_WRITE_TS = 0.0
            report = asyncio.run(api_mod.health_check())
            assert report.get("healthy") is True
        finally:
            api_mod._HEALTH_FILE_LAST_WRITE_TS = orig_ts
