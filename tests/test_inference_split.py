"""Pin the inference.py split: kernel plane and router plane stay separate.

The 2026-08-26 split moved the original monolithic inference.py into:
  - inference_kernel.py — MODEL_LADDER + the ladder walk (kernel plane)
  - inference_router.py — ProviderRouter + providers.yaml (CLI/pipeline plane)
with ``inference`` kept as a re-export facade so every existing import
(``from inference import escalate_with_ladder`` etc.) keeps working.

Hard rule (see tests/test_inference_planes.py and the TWO INFERENCE PLANES
comment): the planes must remain separate. MODEL_LADDER must NOT be pointed
at ProviderRouter — measured Hermes fork latency is p50 ≈ 11.9s,
max ≈ 31.4s (findings/hermes_latency_2026-08-26.md), so a unification that
routes kernel calls through the CLI-backed router is not supported by data.
"""

import inspect
import subprocess
from pathlib import Path

import inference
import inference_kernel
import inference_router

ROOT = Path(__file__).resolve().parent.parent


def test_facade_reexports_kernel_plane():
    assert inference.MODEL_LADDER is inference_kernel.MODEL_LADDER
    assert inference.escalate_with_ladder is inference_kernel.escalate_with_ladder
    assert inference.OllamaInference is inference_kernel.OllamaInference


def test_facade_reexports_router_plane():
    assert inference.ProviderRouter is inference_router.ProviderRouter
    assert inference.load_providers_config is inference_router.load_providers_config
    assert inference.UnknownTaskClassError is inference_router.UnknownTaskClassError


def test_kernel_ladder_lives_in_kernel_module_not_router():
    # MODEL_LADDER is defined in the kernel module...
    assert "MODEL_LADDER" in vars(inference_kernel)
    # ...and the router module never defines its own ladder.
    assert "MODEL_LADDER" not in vars(inference_router)


def test_model_ladder_not_pointed_at_provider_router():
    """The kernel walk must stay on OllamaInference/claude_code rungs —
    no ProviderRouter/endpoint-pool leakage into MODEL_LADDER."""
    for task_type, ladder in inference.MODEL_LADDER.items():
        for rung in ladder:
            model = rung.get("model")
            assert isinstance(model, str), f"{task_type}: non-string model {model!r}"
            assert "router" not in model.lower()
    src = inspect.getsource(inference_kernel)
    # Strip docstrings/comments: only real code references are forbidden.
    import ast
    tree = ast.parse(src)
    code_parts = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Name, ast.Attribute)):
            code_parts.append(ast.dump(node))
    assert not any("ProviderRouter" in p for p in code_parts), (
        "kernel module must not reference ProviderRouter — planes are separate"
    )


def test_escalate_with_ladder_walks_model_ladder():
    src = inspect.getsource(inference_kernel.escalate_with_ladder)
    assert "MODEL_LADDER" in src


def _git_show_head(path: str) -> str:
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout


def test_no_plane_deleted_relative_to_pre_split_monolith():
    """Both planes survive the split: every public name of each plane that
    existed in HEAD's inference.py is still importable from inference."""
    head_src = _git_show_head("inference.py")
    for name in (
        "MODEL_LADDER", "escalate_with_ladder", "OllamaInference",
        "AgentConfig", "AGENT_CONFIGS", "warmup_models",
    ):
        assert name in head_src and hasattr(inference, name), name
    for name in ("ProviderRouter", "load_providers_config", "EndpointConfig"):
        assert name in head_src and hasattr(inference, name), name


def test_two_planes_comment_still_present_and_forbids_unify():
    kernel_src = Path(inference_kernel.__file__).read_text()
    router_src = Path(inference_router.__file__).read_text()
    combined = kernel_src + router_src
    assert "TWO INFERENCE PLANES" in combined
    assert "do not unify" in combined or "Do NOT unify" in combined \
        or "Do not unify" in combined
    assert "hermes_latency_2026-08-26.md" in combined, (
        "the measured-latency rationale (p50 ≈ 11.9s / max ≈ 31.4s) must be cited"
    )


def test_kernel_does_not_import_tools_autonomous():
    for mod in (inference_kernel, inference_router):
        src = inspect.getsource(mod)
        assert "tools.autonomous" not in src
