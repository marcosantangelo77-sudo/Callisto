"""SPEED run 11 — proxy discovery by declared default.

Run 7 built the fast path (persistent local proxy, same Portal model);
this run makes it DISCOVERABLE. Before: ox_alpha_proxy resolved only from
OX_ALPHA_PROXY_BASE_URL, which no production path sets — so out of the box
every model call paid the ~12-20s fresh-fork CLI startup even while the
proxy sat running on loopback. Measured live this run: CLI fork 14.0s /
20.4s / 105.8s per call; warm-proxy band 1.2-2.4s (runs 7/8).

Pins:
1. RESOLUTION — base_url resolves explicit → env → declared
   base_url_default → unresolved; only the last is a behaviour change and
   it applies ONLY to endpoints that declare a default.
2. ENV STILL WINS — an operator-set env var overrides the default.
3. NO DEFAULT, NO CHANGE — endpoints without base_url_default stay exactly
   as before (unresolved when their env is unset).
4. HONEST DEGRADATION — default pointing at a dead loopback port fails
   FAST, records failure + cooldown, and the call still completes via the
   next tier; the cooled endpoint drops out of candidates.
5. NO GATE MOVED — nothing here touches confidence, caching, cutoffs, or
   the adversary's separate call (schema-bearing calls still skip
   non-hermes_cli endpoints without structured_output).
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

REPO = Path(__file__).resolve().parents[1]

PROXY_ENV_VARS = ("OX_ALPHA_PROXY_BASE_URL", "OX_ALPHA_PROXY_API_KEY",
                  "OX_ALPHA_PROXY_MODEL")


def _clear_proxy_env(monkeypatch):
    for v in PROXY_ENV_VARS:
        monkeypatch.delenv(v, raising=False)


def _write_tmp_yaml(tmp_path, *, explicit_base=None,
                    default_base="http://127.0.0.1:9/v1",
                    with_cli=True) -> str:
    """Minimal two-endpoint config: 'p' (openai_compat, discovery under
    test) and 'cli' (hermes_cli fallback). Port 9 + the NoSocket guard make
    any attempt against the default fail instantly and hermetically."""
    line = f"    base_url: {explicit_base}\n" if explicit_base else ""
    cli = ("  cli:\n"
           "    backend: hermes_cli\n"
           "    model: ox-alpha\n") if with_cli else ""
    chain = "[p, cli]" if with_cli else "[p]"
    path = tmp_path / "providers.yaml"
    path.write_text(
        "default_tier: p\n"
        "providers:\n"
        "  p:\n"
        "    backend: openai_compat\n"
        + line +
        f"    base_url_default: {default_base}\n"
        "    model: m\n"
        + cli +
        "routing:\n"
        "  task_classes:\n"
        f"    screening: {chain}\n")
    return str(path)


class TestResolution:
    def test_default_discovered_when_env_unset(self, monkeypatch):
        """The headline behaviour: with NO env vars, the running-proxy
        default in config/providers.yaml resolves the endpoint."""
        _clear_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        ep = r.endpoints["ox_alpha_proxy"]
        assert not ep.extra.get("_unresolved")
        assert ep.base_url == "http://127.0.0.1:8646/v1"
        assert ep.model == "stealth/ox-alpha"

    def test_env_still_wins_over_default(self, monkeypatch):
        _clear_proxy_env(monkeypatch)
        monkeypatch.setenv("OX_ALPHA_PROXY_BASE_URL",
                           "http://127.0.0.1:9999/v1")
        r = inference.ProviderRouter()
        assert r.endpoints["ox_alpha_proxy"].base_url == \
            "http://127.0.0.1:9999/v1"

    def test_explicit_base_url_beats_everything(self, monkeypatch, tmp_path):
        _clear_proxy_env(monkeypatch)
        monkeypatch.setenv("OX_ALPHA_PROXY_BASE_URL",
                           "http://127.0.0.1:9998/v1")
        r = inference.ProviderRouter(config_path=_write_tmp_yaml(
            tmp_path, explicit_base="http://127.0.0.1:7777/v1"))
        assert r.endpoints["p"].base_url == "http://127.0.0.1:7777/v1"

    def test_no_default_stays_unresolved(self, monkeypatch, tmp_path):
        """Regression pin for every OTHER endpoint: without a declared
        default and with its env unset, resolution is unchanged."""
        yaml = tmp_path / "providers.yaml"
        yaml.write_text(
            "default_tier: p\n"
            "providers:\n"
            "  p:\n"
            "    backend: openai_compat\n"
            "    base_url_env: P_BASE_URL\n"
            "    model: m\n"
            "routing:\n"
            "  task_classes:\n"
            "    screening: [p]\n")
        r = inference.ProviderRouter(config_path=str(yaml))
        assert r.endpoints["p"].extra.get("_unresolved")
        assert r.candidates_for("screening") == []


class TestRepoConfig:
    def test_ox_alpha_proxy_declares_loopback_default(self):
        cfg = inference.load_providers_config(
            str(REPO / "config" / "providers.yaml"))
        raw = cfg["providers"]["ox_alpha_proxy"]
        assert raw["base_url_default"].startswith("http://127.0.0.1:"), (
            "the discovery default must be LOOPBACK — never a remote host")

    def test_discovered_proxy_ahead_of_cli_in_every_class(self, monkeypatch):
        _clear_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        for tc, names in r.task_classes.items():
            names = names if isinstance(names, list) else [names]
            if "ox_alpha_proxy" in names:
                assert names.index("ox_alpha_proxy") < names.index("ox_alpha")


class TestHonestDegradation:
    def test_dead_default_fails_fast_and_fails_over(self, monkeypatch,
                                                    tmp_path):
        """Default points at port 9 (NoSocket guard makes the attempt raise
        instantly — hermetic stand-in for connection-refused). The call must
        still COMPLETE on the fallback tier, and the dead endpoint must cool
        out of candidates."""
        _clear_proxy_env(monkeypatch)
        r = inference.ProviderRouter(config_path=_write_tmp_yaml(tmp_path))

        import tools.pipeline.hermes_cli as hc

        class FakeCli:
            async def complete(self, messages, *, role="", binary=None,
                               cwd="/tmp", timeout_s=240.0):
                return {"content": "{\"ok\": true}", "rc": 0, "stderr": ""}

        monkeypatch.setattr(hc, "hermes_complete", FakeCli().complete)

        res = asyncio.run(r.complete(
            "screening", [{"role": "user", "content": 'Reply {"ok": true}'}]))
        assert res["tier"] == "cli"
        st = r.states["p"]
        assert st.consecutive_failures >= 1
        assert "p" not in r.candidates_for("screening"), (
            "a just-failed endpoint must be cooling and skipped")

    def test_schema_bearing_calls_still_skip_the_proxy(self, monkeypatch):
        """The adversary passes schema=...; structured_output=false means the
        proxy must remain invisible to schema-enforcing callers EXACTLY as
        before — the critic keeps its own path untouched."""
        _clear_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        cands = r.candidates_for("adversarial_review", schema={"type": "obj"})
        assert "ox_alpha_proxy" not in cands


class TestAdversaryUntouched:
    def test_adversarial_review_still_resolves(self, monkeypatch):
        _clear_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        assert r.candidates_for("adversarial_review")
