"""Pin the two inference planes so neither is silently removed.

Callisto intentionally has TWO routing planes:

1. Kernel plane: ``inference.MODEL_LADDER`` (task_type -> ordered model
   list), walked by ``inference.complete()`` on every call.
2. CLI/pipeline plane: ``ProviderRouter`` backed by ``config/providers.yaml``
   via ``load_providers_config``.

Canonical *future* routing is ProviderRouter, but the kernel may only be
pointed at it after measuring Hermes CLI fork latency (~14s historically).
Until then both planes are live. These tests pin that both exist; if one
is deleted without a deliberate migration, they fail.
"""

import inference


def test_model_ladder_has_reasoning_key():
    assert "reasoning" in inference.MODEL_LADDER
    ladder = inference.MODEL_LADDER["reasoning"]
    assert isinstance(ladder, list) and len(ladder) > 0


def test_model_ladder_expected_keys():
    expected = {"reasoning", "classification", "review"}
    missing = expected - set(inference.MODEL_LADDER)
    assert not missing, f"MODEL_LADDER lost keys: {missing}"


def test_providers_config_exists_with_at_least_one_provider():
    cfg = inference.load_providers_config()
    providers = cfg.get("providers")
    assert isinstance(providers, dict)
    assert len(providers) >= 1
