"""SPEED run 18 — batch transport selection tests.

Pins the run_retro_batch model-selection seam: proxy default when
ox_alpha_proxy resolves, honest CLI fallback otherwise. No network in any
test; ProviderRouter is stubbed via monkeypatching.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "run_retro_batch", ROOT / "scripts" / "run_retro_batch.py")
rrb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rrb)


class _StubRouter:
    def __init__(self, unresolved: bool):
        from types import SimpleNamespace

        self.endpoints = {"ox_alpha_proxy": SimpleNamespace(
            extra={} if not unresolved else {"_unresolved": True})}


def _patch_router(monkeypatch, router):
    import inference as inf  # noqa: F401 — ensure module exists for patch target

    monkeypatch.setattr(
        sys.modules["inference"], "ProviderRouter", lambda: router)


def test_default_is_proxy_when_resolved(monkeypatch):
    monkeypatch.delenv("CALLISTO_RETRO_TRANSPORT", raising=False)
    _patch_router(monkeypatch, _StubRouter(unresolved=False))
    model = rrb._make_model()
    assert type(model).__name__ == "RouterModel"


def test_cli_fallback_when_unresolved(monkeypatch, capsys):
    monkeypatch.setenv("CALLISTO_RETRO_TRANSPORT", "proxy")
    _patch_router(monkeypatch, _StubRouter(unresolved=True))
    monkeypatch.setattr(rrb, "hermes_available", lambda: True)
    model = rrb._make_model()
    assert isinstance(model, rrb.HermesCliModel)
    assert "fallback" in capsys.readouterr().out


def test_explicit_cli_honoured(monkeypatch):
    monkeypatch.setenv("CALLISTO_RETRO_TRANSPORT", "cli")
    monkeypatch.setattr(rrb, "hermes_available", lambda: True)
    model = rrb._make_model()
    assert isinstance(model, rrb.HermesCliModel)


def test_router_crash_falls_back_to_cli(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("no config")
    monkeypatch.setenv("CALLISTO_RETRO_TRANSPORT", "proxy")
    import inference
    monkeypatch.setattr(inference, "ProviderRouter", _boom)
    monkeypatch.setattr(rrb, "hermes_available", lambda: True)
    model = rrb._make_model()
    assert isinstance(model, rrb.HermesCliModel)
    assert "unavailable" in capsys.readouterr().out


def test_factory_builds_pipeline_researcher_with_selected_model(monkeypatch):
    """The chosen model is what every question actually runs on."""
    monkeypatch.delenv("CALLISTO_RETRO_TRANSPORT", raising=False)
    _patch_router(monkeypatch, _StubRouter(unresolved=False))
    factory = rrb.make_researcher_factory()
    researcher = factory()
    assert type(researcher.model).__name__ == "RouterModel"
    # adversary rides the SAME selected backend object (its own call,
    # same transport) — never silently reverted to a different path.
    assert researcher.adversary_router is researcher.model
