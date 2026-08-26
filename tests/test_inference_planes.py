"""Pin the two inference planes so neither is silently removed.

Callisto intentionally has TWO routing planes:

1. Kernel plane: ``inference.MODEL_LADDER`` (task_type -> ordered model
   list), walked by ``inference.complete()`` on every call.
2. CLI/pipeline plane: ``ProviderRouter`` backed by ``config/providers.yaml``
   via ``load_providers_config``.

Measured Hermes CLI fork latency is p50 ≈ 11.9s / max ≈ 31.4s
(findings/hermes_latency_2026-08-26.md), so unifying onto ProviderRouter
this wave is forbidden. These tests pin that both planes exist, that the
measurement citation stays in inference.py, and that neither
MODEL_LADDER nor complete() names ``hermes_cli`` as a kernel transport.
"""

import inspect

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


def test_inference_source_cites_measured_latency():
    """The latency pin must survive: deleting the measurement silently
    would let someone unify on a sub-10s assumption."""
    src = inspect.getsource(inference)
    has_p50_pin = ("p50" in src and "11.9" in src) or (
        "hermes_latency_2026-08-26.md" in src
    )
    assert has_p50_pin, (
        "inference.py lost its measured-latency pin (p50 11.9s / "
        "findings/hermes_latency_2026-08-26.md); re-add it before touching "
        "the two-plane structure"
    )


def test_supervisor_launches_hermes_as_agent_runtime_not_transport():
    """Hermes is the OX agent runtime (supervisor) — never a completion
    transport inside either inference plane."""
    from pathlib import Path

    sup = Path(inference.__file__).parent / "scripts" / "nous-supervisor.sh"
    if sup.is_file():
        src = sup.read_text(encoding="utf-8")
        assert "-m \"$MODEL\"" in src
        assert "stealth/ox-alpha" in src


def test_kernel_ladder_does_not_use_hermes_cli_transport():
    """MODEL_LADDER entries and complete() must not name hermes_cli as a
    kernel transport. (ProviderRouter may still mention hermes_cli.)"""
    from pathlib import Path

    kernel = Path(inference.__file__).with_name("inference_kernel.py")
    src = kernel.read_text(encoding="utf-8")
    ladder_start = src.index("MODEL_LADDER")
    # First closing brace of the ladder dict assignment, not an import.
    assign = src.index("MODEL_LADDER:", ladder_start)
    ladder_end = src.index("\n\n", assign)
    kernel_src = src[assign:ladder_end]
    assert "hermes_cli" not in kernel_src, "MODEL_LADDER mentions hermes_cli"

    complete_fn = getattr(inference, "complete", None)
    if complete_fn is not None:
        complete_src = inspect.getsource(complete_fn)
        assert "hermes_cli" not in complete_src, "complete() mentions hermes_cli"
    for name in dir(inference):
        obj = getattr(inference, name)
        if callable(obj) and (name.startswith("complete") or "complete_sync" in name):
            s = inspect.getsource(obj)
            assert "hermes_cli" not in s, f"{name} mentions hermes_cli"
