"""SPEED run 17 — persistent dead-hop health memory.

FINDING BEING PINNED: gpu1 (and gpu1_fast) sit FIRST in the routing list of
every task class while the llama-server ports (8080/8081) are DOWN on this
machine. Every fresh ProviderRouter process pays a connect-refused probe
(~0.5s incl. the run-16 immediate-failover rule), then fails over — and
because each CLI/proxy-driven run is a NEW process, the exponential cooldown
that would normally suppress the dead hop never persists. Measured live
tonight: every routed call logged 'endpoint gpu1 failed' before serving.

Fix (correctness-preserving): persist per-endpoint consecutive_failures and
cooldown_until in a small JSON state file under CALLISTO_STATE_DIR (default
~/.local/state/callisto/router_health.json). On startup, load recorded state;
on record_success/record_failure, write through. A hop that was dead seconds
ago in ANOTHER process starts cooling-down instead of re-probed live. Success
anywhere clears it, so a box that comes back up is re-admitted after at most
one full cooldown window — same recovery semantics as today, just not
re-discovered from scratch by every process.

Cutoff safety: this caches TRANSPORT HEALTH only — no question content, no
evidence, no model output crosses any retrodiction boundary.

Pins:
  A. failures persist to disk and are loaded into a new router instance.
  B. a loaded dead hop is skipped (available=False) without any network try.
  C. record_success clears persisted failure state (recovery path).
  D. corrupt/missing file degrades to fresh state (no crash).
  E. env var disabled -> no file written, behaviour byte-identical to today.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference  # noqa: E402

CFG = """
default_tier: gpu1
providers:
  gpu1:
    backend: openai_compat
    base_url: http://localhost:9901/v1
    model: m-27b
    context_tokens: 32768
    structured_output: true
    max_concurrency: 1
  proxy:
    backend: openai_compat
    base_url: http://localhost:9902/v1
    model: m-fast
    context_tokens: 32768
    structured_output: false
    max_concurrency: 4
routing:
  task_classes:
    research_synthesis: [gpu1, proxy]
"""


def _router(tmp_path, state_dir):
    cfg = tmp_path / "pool.yaml"
    cfg.write_text(CFG)
    return inference.ProviderRouter(
        config_path=str(cfg), health_state_dir=str(state_dir))


def test_a_failure_persists_and_loads(tmp_path):
    sd = tmp_path / "state"
    r1 = _router(tmp_path, sd)
    r1.states["gpu1"].record_failure()
    r1._save_health_state()
    assert (sd / "router_health.json").exists()

    r2 = _router(tmp_path, sd)
    # Loaded state: the dead hop must be cooling down WITHOUT any live probe.
    st = r2.states["gpu1"]
    assert st.consecutive_failures == 1
    assert not st.available


def test_b_loaded_dead_hop_skipped_in_candidates(tmp_path):
    sd = tmp_path / "state"
    r1 = _router(tmp_path, sd)
    for _ in range(3):
        r1.states["gpu1"].record_failure()
    r1._save_health_state()

    r2 = _router(tmp_path, sd)
    assert "gpu1" not in r2.candidates_for("research_synthesis")
    assert r2.candidates_for("research_synthesis") == ["proxy"]


def test_c_record_success_clears_persisted_state(tmp_path):
    sd = tmp_path / "state"
    r1 = _router(tmp_path, sd)
    r1.states["gpu1"].record_failure()
    r1.states["gpu1"].record_success()
    r1._save_health_state()

    data = json.loads((sd / "router_health.json").read_text())
    assert data["endpoints"]["gpu1"]["consecutive_failures"] == 0

    r2 = _router(tmp_path, sd)
    assert r2.states["gpu1"].available


def test_d_corrupt_file_degrades_to_fresh(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    (sd / "router_health.json").write_text("{not json")
    r = _router(tmp_path, sd)
    assert r.states["gpu1"].available
    assert r.states["gpu1"].consecutive_failures == 0


def test_e_disabled_by_default_no_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_ROUTER_HEALTH", "0")
    monkeypatch.delenv("CALLISTO_STATE_DIR", raising=False)
    cfg = tmp_path / "pool.yaml"
    cfg.write_text(CFG)
    r = inference.ProviderRouter(config_path=str(cfg))
    # Opt-out flag: nothing written anywhere, behaviour as today.
    r.states["gpu1"].record_failure()
    assert r._health_state_dir is None
    r._save_health_state()  # no-op, must not raise or create anything
    assert not (tmp_path / "router_health.json").exists()


def test_f_write_through_on_complete_failure_path(tmp_path, monkeypatch):
    """complete() against a dead first candidate persists the failure so the
    NEXT PROCESS (new router instance) skips it."""
    import httpx as _h

    sd = tmp_path / "state"
    r1 = _router(tmp_path, sd)

    def _post(client, url, **kw):  # signature-agnostic recorder
        raise _h.ConnectError("refused")

    monkeypatch.setattr(r1, "_post", _post)
    with pytest.raises(Exception):
        import asyncio
        asyncio.run(r1.complete("research_synthesis",
                                [{"role": "user", "content": "x"}],
                                timeout=5))

    r2 = _router(tmp_path, sd)
    assert not r2.states["gpu1"].available
