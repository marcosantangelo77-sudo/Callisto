"""Ox Alpha / Nous Portal login is a first-class health signal.

ChatGPT's workstation workers succeeded because ~/.hermes/auth.json already
held a Nous session. A cloud VM with only the Hermes binary must NOT report
ox_alpha healthy. These tests pin that distinction without touching secrets.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.pipeline import hermes_cli  # noqa: E402
import inference  # noqa: E402


def _write_auth(tmp_path: Path, payload: dict) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text(json.dumps(payload), encoding="utf-8")
    return home


class TestHermesLoggedIn:
    def test_missing_store_is_logged_out(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "missing"))
        assert hermes_cli.hermes_logged_in() is False

    def test_empty_providers_is_logged_out(self, tmp_path, monkeypatch):
        home = _write_auth(tmp_path, {"version": 1, "providers": {}, "credential_pool": {}})
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert hermes_cli.hermes_logged_in() is False

    def test_nous_refresh_token_is_logged_in(self, tmp_path, monkeypatch):
        home = _write_auth(tmp_path, {
            "providers": {
                "nous": {
                    "refresh_token": "dummy-not-a-real-token",
                    "access_token": "dummy-access",
                }
            }
        })
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert hermes_cli.hermes_logged_in() is True

    def test_relogin_required_without_tokens_is_logged_out(self, tmp_path, monkeypatch):
        home = _write_auth(tmp_path, {
            "providers": {
                "nous": {
                    "last_auth_error": {"relogin_required": True},
                }
            }
        })
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert hermes_cli.hermes_logged_in() is False

    def test_credential_pool_entry_counts(self, tmp_path, monkeypatch):
        home = _write_auth(tmp_path, {
            "providers": {},
            "credential_pool": {
                "nous": [{"id": "abc", "auth_type": "oauth", "secret_fingerprint": "sha256:x"}]
            },
        })
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert hermes_cli.hermes_logged_in() is True

    def test_garbage_json_is_logged_out(self, tmp_path, monkeypatch):
        home = tmp_path / "h"
        home.mkdir()
        (home / "auth.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert hermes_cli.hermes_logged_in() is False


class TestRouterHealthHonest:
    def _router(self, tmp_path: Path) -> inference.ProviderRouter:
        cfg = tmp_path / "p.yaml"
        cfg.write_text(
            "providers:\n"
            "  oxa:\n"
            "    backend: hermes_cli\n"
            "routing:\n"
            "  task_classes:\n"
            "    screening: oxa\n"
        )
        return inference.ProviderRouter(config_path=str(cfg))

    def test_binary_without_login_is_unhealthy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hermes_cli, "hermes_available", lambda: True)
        monkeypatch.setattr(hermes_cli, "hermes_logged_in", lambda: False)
        router = self._router(tmp_path)
        res = asyncio.run(router.check_health("oxa"))
        assert res["status"] == "error"
        assert "portal" in res["error"].lower() or "logged" in res["error"].lower()

    def test_binary_and_login_is_healthy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hermes_cli, "hermes_available", lambda: True)
        monkeypatch.setattr(hermes_cli, "hermes_logged_in", lambda: True)
        router = self._router(tmp_path)
        res = asyncio.run(router.check_health("oxa"))
        assert res["status"] == "ok"

    def test_missing_binary_still_names_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hermes_cli, "hermes_available", lambda: False)
        monkeypatch.setattr(hermes_cli, "hermes_logged_in", lambda: False)
        router = self._router(tmp_path)
        res = asyncio.run(router.check_health("oxa"))
        assert res["status"] == "error"
        assert "binary" in res["error"].lower()
