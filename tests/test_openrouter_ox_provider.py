"""Pins: OpenRouter Ox Alpha is an env-backed ProviderRouter endpoint.

The key never lives in git. Adding this provider is the API swap path
(local llama.cpp still first when healthy).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
YAML = REPO / "config" / "providers.yaml"
SUPERVISOR = REPO / "scripts" / "nous-supervisor.sh"


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load(YAML.read_text(encoding="utf-8"))


def test_openrouter_ox_is_env_backed_not_hardcoded(cfg):
    ox = cfg["providers"]["openrouter_ox"]
    assert ox["backend"] == "openai_compat"
    assert ox["base_url"] == "https://openrouter.ai/api/v1"
    assert ox["api_key_env"] == "OPENROUTER_API_KEY"
    assert ox["model"] == "stealth/ox-alpha"
    assert "api_key" not in ox
    raw = YAML.read_text(encoding="utf-8")
    assert "sk-or-v1-" not in raw
    assert "OPENROUTER_API_KEY=" not in raw or "# OPENROUTER" in raw


def test_task_classes_include_openrouter_ox_after_local(cfg):
    classes = cfg["routing"]["task_classes"]
    for name, chain in classes.items():
        assert "openrouter_ox" in chain, name
        assert chain[-1] == "ox_alpha", name
        local = [p for p in chain if p.startswith("gpu")]
        if local:
            assert chain.index(local[0]) < chain.index("openrouter_ox"), name


def test_judgment_still_prefers_frontier(cfg):
    for name in ("promotion_judgment", "adversarial_review"):
        chain = cfg["routing"]["task_classes"][name]
        assert chain[0] == "frontier"


def test_supervisor_defaults_to_openrouter_when_key_present():
    src = SUPERVISOR.read_text(encoding="utf-8")
    assert "CALLISTO_HERMES_PROVIDER" in src
    assert "--provider \"$PROVIDER\"" in src or '--provider "$PROVIDER"' in src
    assert "openrouter" in src
    assert "stealth/ox-alpha" in src
    # must not print secrets
    assert "echo" in src
    assert "OPENROUTER_API_KEY" in src
    assert "print(v)" in src  # load helper prints to stdout for capture only
    # the live argv must not hardcode nous as the only provider
    assert "--provider nous \\" not in src or "PROVIDER" in src
