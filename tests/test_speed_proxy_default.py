"""SPEED run 9 (2026-08-24): the persistent proxy participates in routing
WITHOUT an exported env var.

Measured live before this run: zero running processes had
OX_ALPHA_PROXY_BASE_URL set, so `ox_alpha_proxy` resolved _unresolved on
every machine where nobody exported it — candidates_for() returned
['gpu1', 'ox_alpha'] and EVERY completion paid the fresh-fork CLI tax
(11.30s measured for a trivial call) while the proxy sat idle on
127.0.0.1:8645/8646.

The fix is two pieces:
  1. providers.yaml: ox_alpha_proxy gains a static default base_url.
  2. inference._endpoint_from_config: an env var that IS set overrides the
     static value; unset env leaves the default standing (before run 9 a
     static value shadowed the env var entirely).

Pins below. No caching anywhere near a cutoff; no gate moved; the adversary
keeps its own separate call (it passes schema=..., so it never used this
endpoint — structured_output stays false).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8645/v1"


def _no_proxy_env(monkeypatch) -> None:
    for v in ("OX_ALPHA_PROXY_BASE_URL", "OX_ALPHA_PROXY_API_KEY",
              "OX_ALPHA_PROXY_MODEL"):
        monkeypatch.delenv(v, raising=False)


class TestResolution:
    def test_default_resolves_without_env(self, monkeypatch):
        _no_proxy_env(monkeypatch)
        ep = inference.ProviderRouter().endpoints["ox_alpha_proxy"]
        assert not ep.extra.get("_unresolved")
        assert ep.base_url == DEFAULT_URL
        assert ep.model == "stealth/ox-alpha"

    def test_env_set_overrides_static_default(self, monkeypatch):
        """Precedence pin: env-if-set wins; the static value must NOT
        shadow it (the pre-run-9 code consulted env only when static was
        absent)."""
        monkeypatch.setenv("OX_ALPHA_PROXY_BASE_URL", "http://127.0.0.1:9999/v1")
        ep = inference.ProviderRouter().endpoints["ox_alpha_proxy"]
        assert ep.base_url == "http://127.0.0.1:9999/v1"
        assert not ep.extra.get("_unresolved")

    def test_env_empty_string_leaves_default(self, monkeypatch):
        monkeypatch.setenv("OX_ALPHA_PROXY_BASE_URL", "")
        ep = inference.ProviderRouter().endpoints["ox_alpha_proxy"]
        assert ep.base_url == DEFAULT_URL
        assert not ep.extra.get("_unresolved")

    def test_frontier_still_unresolved_without_env(self, monkeypatch):
        """Byte-identical resolution for entries with NO static base_url."""
        _no_proxy_env(monkeypatch)
        monkeypatch.delenv("FRONTIER_BASE_URL", raising=False)
        ep = inference.ProviderRouter().endpoints["frontier"]
        assert ep.extra.get("_unresolved")

    def test_gpu1_resolution_unchanged(self, monkeypatch):
        _no_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        assert not r.endpoints["gpu1"].extra.get("_unresolved")
        assert r.endpoints["gpu1"].base_url == "http://localhost:8080/v1"


class TestRouting:
    def test_proxy_in_candidates_without_env(self, monkeypatch):
        _no_proxy_env(monkeypatch)
        cands = inference.ProviderRouter().candidates_for("research_synthesis")
        assert "ox_alpha_proxy" in cands
        assert cands.index("ox_alpha_proxy") < cands.index("ox_alpha")
        assert cands[-1] == "ox_alpha"

    def test_all_task_classes_keep_cli_last_without_env(self, monkeypatch):
        _no_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        for tc, names in r.task_classes.items():
            names = names if isinstance(names, list) else [names]
            if "ox_alpha_proxy" in names:
                assert names.index("ox_alpha_proxy") < names.index("ox_alpha"), tc
            assert names[-1] == "ox_alpha", tc


class TestDegradation:
    def test_dead_default_port_fails_over_fast(self, monkeypatch):
        """Proxy process absent => refused connect (<20ms measured) + one
        bounded in-place retry => failover lands on ox_alpha exactly as
        pre-run-9. Hermetic: dead port + fake CLI, no real fork."""
        _no_proxy_env(monkeypatch)
        # point the DEFAULT at a guaranteed-dead port by overriding the env
        # (same failure mode as an absent proxy process on the default port;
        # keeps the test hermetic without editing the yaml)
        monkeypatch.setenv("OX_ALPHA_PROXY_BASE_URL", "http://127.0.0.1:9/v1")
        r = inference.ProviderRouter()
        for name in ("gpu1", "gpu1_fast", "frontier"):
            if name in r.states:
                for _ in range(4):
                    r.states[name].record_failure()

        import tools.pipeline.hermes_cli as hc

        class FakeCli:
            async def complete(self, messages, *, role="", binary=None,
                               cwd="/tmp", timeout_s=240.0):
                return {"content": "{\"ok\": true}", "rc": 0, "stderr": ""}

        monkeypatch.setattr(hc, "hermes_complete", FakeCli().complete)
        res = asyncio.run(r.complete(
            "research_synthesis",
            [{"role": "user", "content": "Reply {\"ok\": true}"}]))
        assert res["tier"] == "ox_alpha"
        assert r.states["ox_alpha_proxy"].consecutive_failures >= 1

    def test_adversary_capability_filter_unchanged(self, monkeypatch):
        """Schema-bearing callers (agp.adversary) still skip the proxy — its
        structured_output declaration stayed false. The adversary's own call
        is untouched."""
        _no_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        assert r.endpoints["ox_alpha_proxy"].structured_output is False
        schema = {"type": "object"}
        cands = r.candidates_for("adversarial_review", schema=schema)
        assert "ox_alpha_proxy" not in cands
