"""Autofill 0059 — dual inference planes (characterization).

Characterizes the TWO INFERENCE PLANES contract that Callisto deliberately
maintains:

1. Kernel plane: ``inference_kernel.MODEL_LADDER`` + ``complete()`` /
   ``escalate_with_ladder()`` — the task_type -> ordered model ladder walked
   for every Ollama-backed completion call.
2. CLI/pipeline plane: ``ProviderRouter`` (inference_router.py) backed by
   ``config/providers.yaml`` via ``load_providers_config`` — endpoint-pool
   routing used by the pipeline.

Pins under test here:

* MODEL_LADDER must NOT mention ``hermes_cli`` or ``ProviderRouter`` —
  Hermes is the agent runtime (supervisor), never a completion transport,
  and the kernel plane must not reach into the router plane's vocabulary.
* gpu1 is a ``llama_cpp_server`` endpoint (the quality-first local tier).
* ``openrouter_ox`` is an ``openai_compat``, env-backed provider — the key
  never lives in git and never hardcodes anything but a placeholder.
* The two planes stay UNIFIED-BY-NOTHING: neither module imports the other,
  and the measured-latency justification (p50 ≈ 11.9s Hermes fork) stays
  cited in source.
* The live-betting gate stays shut: ``_PAPER_TRADE_SIGNAL_STATUSES`` is
  exactly {paper_trading}, and ``generate_paper_trade_signal`` returns []
  for any other status (including "live") before touching odds.

These tests characterize current behavior. If one fails after an unrelated
change, treat it as a regression against this contract, not noise.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "inference_kernel.py"
ROUTER = REPO / "inference_router.py"
INFERENCE = REPO / "inference.py"
PROVIDERS_YAML = REPO / "config" / "providers.yaml"
SUPERVISOR = REPO / "scripts" / "nous-supervisor.sh"
LATENCY_FINDING = REPO / "findings" / "hermes_latency_2026-08-26.md"

FORBIDDEN_IN_LADDER = ("hermes_cli", "ProviderRouter", "provider_router")


# ── helpers ──────────────────────────────────────────────────────────────────


def _kernel_src() -> str:
    return KERNEL.read_text(encoding="utf-8")


def _router_src() -> str:
    return ROUTER.read_text(encoding="utf-8")


def _ladder_block() -> str:
    """Source text of the MODEL_LADDER assignment only."""
    src = _kernel_src()
    start = src.index("MODEL_LADDER:")
    # end at the closing brace of the dict literal
    depth = 0
    i = src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError("MODEL_LADDER literal not terminated")


def _ladder_models() -> list[str]:
    import sys

    sys.path.insert(0, str(REPO))
    import inference

    models: list[str] = []
    for rungs in inference.MODEL_LADDER.values():
        for rung in rungs:
            models.append(rung["model"])
    return models


@pytest.fixture(scope="module")
def providers_cfg():
    return yaml.safe_load(PROVIDERS_YAML.read_text(encoding="utf-8"))


# ── 1. MODEL_LADDER purity: no hermes_cli, no ProviderRouter ────────────────


def test_ladder_block_does_not_mention_hermes_cli():
    assert "hermes_cli" not in _ladder_block()


def test_ladder_block_does_not_mention_provider_router():
    assert "ProviderRouter" not in _ladder_block()
    assert "provider_router" not in _ladder_block()


def test_no_forbidden_token_anywhere_in_ladder_block():
    block = _ladder_block()
    for token in FORBIDDEN_IN_LADDER:
        assert token not in block, f"MODEL_LADDER mentions {token!r}"


def test_resolved_model_names_are_clean_of_hermes_cli():
    """The *evaluated* ladder values (post name-resolution) are what actually
    routes; none of them may be hermes_cli either."""
    models = _ladder_models()
    assert models, "MODEL_LADDER resolved to nothing"
    for m in models:
        assert m != "hermes_cli"


def test_ladder_is_dict_of_nonempty_lists():
    import sys

    sys.path.insert(0, str(REPO))
    import inference

    assert isinstance(inference.MODEL_LADDER, dict)
    assert len(inference.MODEL_LADDER) >= 5
    for key, rungs in inference.MODEL_LADDER.items():
        assert isinstance(rungs, list) and rungs, key
        for rung in rungs:
            assert set(rung) >= {"model", "quality", "timeout"}, (key, rung)


def test_ladder_core_task_types_present():
    import sys

    sys.path.insert(0, str(REPO))
    import inference

    expected = {
        "reasoning",
        "classification",
        "review",
        "code_generation",
        "hypothesis_gen",
        "deep_work",
    }
    missing = expected - set(inference.MODEL_LADDER)
    assert not missing, f"MODEL_LADDER lost keys: {missing}"


def test_every_rung_has_positive_finite_timeout():
    import sys

    sys.path.insert(0, str(REPO))
    import inference

    for key, rungs in inference.MODEL_LADDER.items():
        for rung in rungs:
            t = rung["timeout"]
            assert isinstance(t, int) and 0 < t <= 600, (key, rung)


def test_frontier_rung_only_first_in_its_chain():
    """claude_code is the frontier head; it should lead its chains, not hide
    mid-ladder behind local tiers (except as deliberate last resort)."""
    import sys

    sys.path.insert(0, str(REPO))
    import inference

    for key, rungs in inference.MODEL_LADDER.items():
        positions = [i for i, r in enumerate(rungs) if r["model"] == "claude_code"]
        for pos in positions:
            assert pos == 0 or pos == len(rungs) - 1, (
                f"{key}: claude_code at index {pos} is neither head nor last resort"
            )


# ── 2. Kernel module hygiene: router stays out of the kernel ────────────────


def test_kernel_does_not_import_inference_router():
    tree = ast.parse(_kernel_src())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "inference_router"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "inference_router"


def test_router_does_not_define_a_model_ladder():
    src = _router_src()
    assert "MODEL_LADDER:" not in src
    assert "MODEL_LADDER =" not in src


def test_planes_live_in_separate_files():
    assert KERNEL.is_file()
    assert ROUTER.is_file()
    assert KERNEL.resolve() != ROUTER.resolve()


def test_inference_reexports_both_plane_entrypoints():
    import sys

    sys.path.insert(0, str(REPO))
    import inference

    # kernel walk entrypoint
    assert "async def escalate_with_ladder(" in _kernel_src()
    assert hasattr(inference, "escalate_with_ladder")
    assert hasattr(inference, "load_providers_config")


def test_latency_citation_still_in_kernel_source():
    """The measured-latency pin is the reason the planes are not unified."""
    src = _kernel_src()
    pinned = (
        ("p50" in src and "11.9" in src)
        or "hermes_latency_2026-08-26.md" in src
    )
    assert pinned, "kernel lost the p50 ≈ 11.9s measured-latency pin"


def test_two_planes_note_present_in_kernel_docstring_or_body():
    src = _kernel_src().lower()
    assert "two inference planes" in src or "inference planes" in src


def test_do_not_unify_comment_survives():
    src = _kernel_src()
    assert "do not unify" in src.lower(), "the DO-NOT-UNIFY warning was removed"


def test_latency_finding_document_exists():
    """The citation target itself must exist so the pin is checkable."""
    assert LATENCY_FINDING.is_file(), LATENCY_FINDING


# ── 3. Router plane: gpu1 is llama_cpp_server; ox endpoints honest ──────────


def test_gpu1_backend_is_llama_cpp_server(providers_cfg):
    gpu1 = providers_cfg["providers"]["gpu1"]
    assert gpu1["backend"] == "llama_cpp_server"
    assert gpu1["base_url"].startswith("http://localhost")
    assert gpu1["context_tokens"] >= 8192


def test_default_tier_is_gpu1(providers_cfg):
    assert providers_cfg["default_tier"] == "gpu1"


def test_openrouter_ox_is_openai_compat_env_backed(providers_cfg):
    ox = providers_cfg["providers"]["openrouter_ox"]
    assert ox["backend"] == "openai_compat"
    assert ox["base_url"] == "https://openrouter.ai/api/v1"
    assert ox["api_key_env"] == "OPENROUTER_API_KEY"
    assert ox["model"] == "stealth/ox-alpha"
    assert "api_key" not in ox, "key material must not be inline"


def test_providers_yaml_contains_no_secret_material():
    raw = PROVIDERS_YAML.read_text(encoding="utf-8")
    assert "sk-or-v1-" not in raw
    assert "sk-ant-" not in raw
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("api_key:") or s.startswith("key:"):
            pytest.fail(f"possible inline credential: {line!r}")


def test_openrouter_ox_in_every_task_class_chain(providers_cfg):
    classes = providers_cfg["routing"]["task_classes"]
    assert classes
    for name, chain in classes.items():
        assert "openrouter_ox" in chain, name
        assert chain[-1] == "ox_alpha", f"{name}: last-resort failover must be ox_alpha"


def test_local_tiers_precede_openrouter_ox(providers_cfg):
    """Local GPU still wins when healthy — the API path is the fallback."""
    classes = providers_cfg["routing"]["task_classes"]
    for name, chain in classes.items():
        local = [p for p in chain if p.startswith("gpu")]
        if local:
            assert chain.index(local[0]) < chain.index("openrouter_ox"), name


def test_judgment_classes_lead_with_frontier(providers_cfg):
    classes = providers_cfg["routing"]["task_classes"]
    for name in ("promotion_judgment", "adversarial_review"):
        assert classes[name][0] == "frontier", name


def test_unknown_task_class_error_exists_and_is_keyerror():
    import sys

    sys.path.insert(0, str(REPO))
    from inference_router import UnknownTaskClassError

    assert issubclass(UnknownTaskClassError, KeyError)


def test_alias_map_bridges_legacy_names_to_canonical():
    import sys

    sys.path.insert(0, str(REPO))
    from inference_router import TASK_CLASS_ALIASES

    canonical = {
        "research_synthesis",
        "hypothesis_generation",
        "adversarial_review",
    }
    bridged = set(TASK_CLASS_ALIASES.values())
    assert canonical & bridged == canonical
    # legacy call-site names map forward, not to themselves
    for src_name, dst in TASK_CLASS_ALIASES.items():
        assert src_name != dst


# ── 4. Planes not unified: structural separation pins ───────────────────────


def test_kernel_defines_ladder_walk_not_router():
    src = _kernel_src()
    assert "async def escalate_with_ladder(" in src
    assert "class ProviderRouter" not in src


def test_router_defines_endpoint_pool_routing_not_ladder_walk():
    src = _router_src()
    assert "class ProviderRouter" in src
    assert "async def complete(" in src
    # the only allowed mention of the kernel walk is in the do-not-unify note;
    # the router must never call it.
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)]
    assert not any(
        getattr(n.func, "id", "") == "escalate_with_ladder"
        or getattr(n.func, "attr", "") == "escalate_with_ladder"
        for n in calls
    )


def test_neither_plane_names_the_other_as_transport():
    k, r = _kernel_src(), _router_src()
    # kernel must not construct ProviderRouter
    assert "ProviderRouter(" not in k
    # router must not walk MODEL_LADDER
    assert "MODEL_LADDER.get" not in r
    assert "MODEL_LADDER[" not in r


def test_supervisor_is_runtime_not_transport():
    """Hermes runs as the agent runtime (supervisor); completions stay HTTP
    through the two planes. The supervisor script must keep `-m "$MODEL"` and
    the stealth/ox-alpha model identity."""
    if SUPERVISOR.is_file():
        src = SUPERVISOR.read_text(encoding="utf-8")
        assert '-m \\"$MODEL\\"' in src or '-m "$MODEL"' in src
        assert "stealth/ox-alpha" in src
        assert "CALLISTO_HERMES_PROVIDER" in src or "openrouter" in src


# ── 5. Live-betting gate: FAIL CLOSED characterization ──────────────────────


def test_paper_signal_statuses_exactly_paper_trading():
    import sys

    sys.path.insert(0, str(REPO))
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})


def test_live_is_not_an_allowed_paper_status():
    import sys

    sys.path.insert(0, str(REPO))
    from tools.signals.paper import allowed_paper_statuses

    statuses = allowed_paper_statuses()
    assert "live" not in statuses
    assert statuses == frozenset({"paper_trading"})


def test_reject_non_paper_rejects_live_and_junk():
    import sys

    sys.path.insert(0, str(REPO))
    from tools.signals.paper import reject_non_paper

    for status in ("live", "", None, "paper_trading ", "LIVE", 123):
        assert reject_non_paper(status) == (status != "paper_trading"), repr(status)
    with pytest.raises(TypeError):
        reject_non_paper({})
    with pytest.raises(TypeError):
        reject_non_paper(["paper_trading"])
    assert reject_non_paper("paper_trading") is False


def test_generate_paper_trade_signal_signature_unchanged():
    """Pin the method signature so widening it to accept a status override
    shows up as a characterization failure."""
    import sys

    sys.path.insert(0, str(REPO))
    from tools.backtest import BacktestEngine

    sig = inspect.signature(BacktestEngine.generate_paper_trade_signal)
    params = [
        (p.name, p.kind)
        for p in sig.parameters.values()
        if p.name != "self"
    ]
    assert params == [
        ("hypothesis_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("live_odds", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ]


def test_generate_paper_trade_signal_returns_empty_for_live(monkeypatch):
    """Behavioral: a 'live' hypothesis gets [] BEFORE any odds processing."""
    import sys

    sys.path.insert(0, str(REPO))
    from tools.backtest import BacktestEngine

    engine = BacktestEngine.__new__(BacktestEngine)

    class BoomManager:
        async def get_hypothesis(self, hid):
            return {"status": "live", "model_config": {}, "edge_threshold": 0.05}

    engine.hypothesis_manager = BoomManager()

    import sys

    sys.path.insert(0, str(REPO))
    from tools.backtest import BacktestEngine

    engine = BacktestEngine.__new__(BacktestEngine)

    class BoomManager:
        async def get_hypothesis(self, hid):
            return {"status": "live", "model_config": {}, "edge_threshold": 0.05}

    # If the gate were bypassed, downstream odds processing would blow up on
    # this manager returning a hypothesis with no usable model_config.
    class ExplodingOdds:
        def get(self, *a, **k):  # pragma: no cover
            raise AssertionError("gate bypassed: reached odds parsing")

    engine.hypothesis_manager = BoomManager()

    import asyncio

    result = asyncio.run(
        engine.generate_paper_trade_signal("h1", ExplodingOdds())
    )
    assert result == []


def test_generate_paper_trade_signal_returns_empty_for_missing(monkeypatch):
    import sys, asyncio

    sys.path.insert(0, str(REPO))
    from tools.backtest import BacktestEngine

    engine = BacktestEngine.__new__(BacktestEngine)

    class NoneManager:
        async def get_hypothesis(self, hid):
            return None

    engine.hypothesis_manager = NoneManager()
    result = asyncio.run(engine.generate_paper_trade_signal("nope", {"games": []}))
    assert result == []


def test_gate_module_docstring_declares_sole_definition():
    src = (REPO / "tools" / "signals" / "paper.py").read_text(encoding="utf-8")
    assert "ONLY definition" in src
    lowered = src.lower()
    assert '"live"' in src and "never" in lowered


# ── 6. Cross-plane consistency ───────────────────────────────────────────────


def test_local_models_referenced_by_router_exist_in_config(providers_cfg):
    classes = providers_cfg["routing"]["task_classes"]
    known = set(providers_cfg["providers"])
    for name, chain in classes.items():
        unknown = [p for p in chain if p not in known]
        assert not unknown, f"{name}: unknown endpoints {unknown}"


def test_kernel_ladder_model_tokens_are_strings():
    block = _ladder_block()
    tree = ast.parse(block, mode="exec")
    assign = tree.body[0]
    assert isinstance(assign, ast.AnnAssign)
    value = assign.value
    assert isinstance(value, ast.Dict)
    for rung_list in value.values:
        assert isinstance(rung_list, ast.List)
        for rung in rung_list.elts:
            assert isinstance(rung, ast.Dict)
            model_node = next(
                v for k, v in zip(rung.keys, rung.values) if getattr(k, "value", None) == "model"
            )
            assert isinstance(model_node, (ast.Constant, ast.Name)), ast.dump(model_node)


def test_budget_cap_present_and_bounded(providers_cfg):
    budget = providers_cfg["routing"].get("budget", {})
    usd = budget.get("usd")
    assert isinstance(usd, (int, float)) and 0 < usd <= 50.0


def test_sensitive_context_stays_local_flag_on(providers_cfg):
    esc = providers_cfg["routing"].get("escalation", {})
    assert esc.get("sensitive_context_stays_local") is True
