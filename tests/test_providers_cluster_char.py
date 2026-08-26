"""Characterization tests: cluster-as-config + role-specialized ProviderRouter.

Pins the multi-model research harness contract (2026-08-26):

* Adding compute (second GPU box, DGX Spark, Mac Studio, LAN llama.cpp
  server) is a CONFIG ENTRY in config/providers.yaml -- documented recipes,
  no code changes.
* The SOTA supervisor is `frontier`, env-backed; swapping hosted APIs is
  env vars, not a code rewrite.
* Role specialization: hard judgment classes prefer frontier first;
  high-volume grind classes prefer local GPU-fast endpoints; every class
  degrades to ox_alpha last so a laptop with no GPU still runs.
* The two inference planes stay separate: the kernel MODEL_LADDER is never
  pointed at ProviderRouter / hermes_cli (pinned in test_inference_planes).

These are characterization tests: they describe what IS, so any change to
the provider/routing contract shows up as an intentional diff here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML ships with the project
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_YAML = REPO_ROOT / "config" / "providers.yaml"
LOOP_QUALITY = REPO_ROOT / "tools" / "loop_quality.py"

CANONICAL_TASK_CLASSES = [
    "hypothesis_generation",
    "research_synthesis",
    "screening",
    "extraction",
    "classification",
    "backtest_interpretation",
    "promotion_judgment",
    "adversarial_review",
]

JUDGMENT_CLASSES = ["promotion_judgment", "adversarial_review"]
HIGH_VOLUME_CLASSES = ["screening", "extraction", "classification"]
LOCAL_PROVIDERS = {"gpu1", "gpu1_fast"}


def _load_yaml() -> dict:
    assert PROVIDERS_YAML.exists(), f"{PROVIDERS_YAML} missing"
    text = PROVIDERS_YAML.read_text(encoding="utf-8")
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def providers_doc() -> dict:
    if yaml is None:
        pytest.skip("PyYAML not available")
    doc = _load_yaml()
    assert isinstance(doc, dict)
    return doc


@pytest.fixture(scope="module")
def routing(providers_doc) -> dict:
    routing = providers_doc.get("routing") or {}
    task_classes = routing.get("task_classes") or {}
    assert isinstance(task_classes, dict) and task_classes, (
        "routing.task_classes must be a non-empty mapping"
    )
    return routing


# ---------------------------------------------------------------------------
# 1. Cluster-as-config: adding compute is documented as new providers entries
# ---------------------------------------------------------------------------


class TestClusterAsConfigDocumentation:
    def test_yaml_documents_scaling_recipes(self):
        """The header comments must teach 'add compute = add an entry'."""
        text = PROVIDERS_YAML.read_text(encoding="utf-8")
        lowered = text.lower()
        # The scaling-recipe comment block mentions concrete expansion paths.
        assert "dgx spark" in lowered or "dgx" in lowered, (
            "providers.yaml comments should document DGX Spark as a recipe"
        )
        assert "recipe" in lowered or "copy an entry" in lowered, (
            "providers.yaml should describe the copy-an-entry scaling recipe"
        )

    @pytest.mark.parametrize(
        "token",
        ["spark", "3090", "5090"],
    )
    def test_expansion_hardware_mentioned_in_comments(self, token: str):
        lowered = PROVIDERS_YAML.read_text(encoding="utf-8").lower()
        assert token in lowered, (
            f"expected '{token}' mentioned in providers.yaml cluster examples/comments"
        )


# ---------------------------------------------------------------------------
# 2. Canonical routing.task_classes keys
# ---------------------------------------------------------------------------


class TestCanonicalTaskClasses:
    def test_all_canonical_classes_declared(self, routing):
        task_classes = routing["task_classes"]
        missing = [c for c in CANONICAL_TASK_CLASSES if c not in task_classes]
        assert not missing, f"task_classes missing canonical keys: {missing}"

    def test_every_class_maps_to_a_list(self, routing):
        for name, tiers in routing["task_classes"].items():
            assert isinstance(tiers, list) and len(tiers) >= 1, (
                f"task_class {name!r} must map to a non-empty list, got {tiers!r}"
            )

    def test_every_referenced_provider_is_defined(self, providers_doc, routing):
        defined = set((providers_doc.get("providers") or {}).keys())
        for name, tiers in routing["task_classes"].items():
            unknown = [t for t in tiers if t not in defined]
            assert not unknown, (
                f"task_class {name!r} references undefined providers: {unknown}"
            )


# ---------------------------------------------------------------------------
# 3. Judgment classes prefer frontier (SOTA supervisor) first
# ---------------------------------------------------------------------------


class TestFrontierFirstForJudgment:
    @pytest.mark.parametrize("cls", JUDGMENT_CLASSES)
    def test_frontier_first(self, routing, cls):
        tiers = routing["task_classes"][cls]
        assert tiers[0] == "frontier", (
            f"{cls} should prefer frontier (SOTA supervisor) first; got {tiers[0]!r}"
        )

    @pytest.mark.parametrize("cls", JUDGMENT_CLASSES)
    def test_local_fallback_after_frontier(self, routing, cls):
        tiers = routing["task_classes"][cls]
        assert any(t in LOCAL_PROVIDERS for t in tiers), (
            f"{cls} must fall back to a local endpoint when frontier fails/budget spent"
        )


# ---------------------------------------------------------------------------
# 4. High-volume grind classes stay local-first, never frontier-first
# ---------------------------------------------------------------------------


class TestHighVolumeStaysLocal:
    @pytest.mark.parametrize("cls", HIGH_VOLUME_CLASSES)
    def test_gpu1_fast_first(self, routing, cls):
        tiers = routing["task_classes"][cls]
        assert tiers[0] == "gpu1_fast", (
            f"{cls} is high-volume grind; expected gpu1_fast first, got {tiers[0]!r}"
        )

    @pytest.mark.parametrize("cls", HIGH_VOLUME_CLASSES)
    def test_frontier_not_preferred(self, routing, cls):
        tiers = routing["task_classes"][cls]
        assert "frontier" not in tiers[:2], (
            f"{cls} must not lean on paid frontier early; order={tiers}"
        )


# ---------------------------------------------------------------------------
# 5. ox_alpha is always the terminal fallback (laptop-with-no-GPU degrades)
# ---------------------------------------------------------------------------


class TestOxAlphaLastResort:
    def test_ox_alpha_terminal_for_every_class(self, routing):
        offenders = {
            name: tiers
            for name, tiers in routing["task_classes"].items()
            if "ox_alpha" not in tiers
        }
        assert not offenders, (
            f"every task_class must end with ox_alpha failover; missing in {sorted(offenders)}"
        )

    @pytest.mark.parametrize(
        "proxy_cls",
        CANONICAL_TASK_CLASSES,
    )
    def test_nothing_after_ox_alpha(self, routing, proxy_cls):
        tiers = routing["task_classes"][proxy_cls]
        assert tiers[-1] == "ox_alpha", (
            f"{proxy_cls}: ox_alpha must be LAST; got tail {tiers[-3:]}"
        )


# ---------------------------------------------------------------------------
# 6. frontier is env-backed: swapping hosted APIs is config/env, not code
# ---------------------------------------------------------------------------


class TestFrontierEnvBacked:
    def _provider(self, providers_doc, name="frontier"):
        provs = providers_doc.get("providers") or {}
        assert name in provs, f"providers.{name} must exist"
        return provs[name]

    def test_frontier_env_keys_present(self, providers_doc):
        frontier = self._provider(providers_doc)
        for key in ("base_url_env", "api_key_env", "model_env"):
            assert key in frontier, (
                f"frontier must declare {key}; API swap is env, not code"
            )
            val = frontier[key]
            assert isinstance(val, str) and val.strip(), f"frontier.{key} empty"

    def test_frontier_env_names_are_valid_identifiers(self, providers_doc):
        frontier = self._provider(providers_doc)
        ident = re.compile(r"^[A-Z][A-Z0-9_]*$")
        for key in ("base_url_env", "api_key_env", "model_env"):
            assert ident.match(frontier[key]), (
                f"frontier.{key}={frontier[key]!r} is not a valid env var name"
            )

    def test_frontier_has_no_hardcoded_secret_or_url(self, providers_doc):
        frontier = self._provider(providers_doc)
        assert "api_key" not in frontier or frontier.get("api_key") in (None, ""), (
            "never inline an API key in providers.yaml"
        )
        assert "base_url" not in frontier, (
            "frontier base URL comes from env (base_url_env), keep it unset here"
        )


# ---------------------------------------------------------------------------
# 7. Local endpoints use llama_cpp_server backend
# ---------------------------------------------------------------------------


class TestLocalBackendIsLlamaCppServer:
    @pytest.mark.parametrize("name", ["gpu1", "gpu1_fast"])
    def test_backend(self, providers_doc, name):
        provs = providers_doc.get("providers") or {}
        entry = provs.get(name)
        assert entry is not None, f"providers.{name} must exist"
        assert entry.get("backend") == "llama_cpp_server", (
            f"{name}.backend must be llama_cpp_server (local plane), got "
            f"{entry.get('backend')!r}"
        )


# ---------------------------------------------------------------------------
# 8. loop_quality phase -> task_class wiring matches providers.yaml
# ---------------------------------------------------------------------------


def _loop_phase_map() -> dict:
    assert LOOP_QUALITY.exists(), f"{LOOP_QUALITY} missing"
    import importlib.util

    import sys

    spec = importlib.util.spec_from_file_location("loop_quality_char", LOOP_QUALITY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves string annotations via this
    spec.loader.exec_module(module)
    mapping = getattr(module, "LOOP_PHASE_TASK_CLASSES", None)
    assert isinstance(mapping, dict), "LOOP_PHASE_TASK_CLASSES must be a dict"
    return mapping


class TestLoopPhaseTaskClasses:
    @pytest.mark.parametrize(
        ("phase", "expected"),
        [
            ("framing", "promotion_judgment"),
            ("evidence_grind", "extraction"),
            ("adversarial_review", "adversarial_review"),
        ],
    )
    def test_phase_mapping(self, phase, expected):
        mapping = _loop_phase_map()
        assert phase in mapping, f"LOOP_PHASE_TASK_CLASSES missing phase {phase!r}"
        assert mapping[phase] == expected, (
            f"phase {phase!r} should route to {expected!r}, got {mapping[phase]!r}"
        )

    @pytest.mark.parametrize(
        ("phase", "expected"),
        [
            ("framing", "promotion_judgment"),
            ("evidence_grind", "extraction"),
            ("adversarial_review", "adversarial_review"),
        ],
    )
    def test_mapped_class_declared_in_yaml(self, providers_doc, phase, expected):
        declared = (providers_doc.get("routing", {}).get("task_classes") or {})
        assert expected in declared, (
            f"phase {phase!r} maps to task_class {expected!r}, which providers.yaml "
            "does not declare"
        )


# ---------------------------------------------------------------------------
# 9. Kernel MODEL_LADDER stays off ProviderRouter/hermes_cli
# ---------------------------------------------------------------------------


class TestKernelLadderUntouched:
    def _inference_src(self) -> str:
        path = REPO_ROOT / "inference_kernel.py"
        if path.exists():
            return path.read_text(encoding="utf-8")
        alt = REPO_ROOT / "inference.py"
        assert alt.exists(), "neither inference_kernel.py nor inference.py found"
        return alt.read_text(encoding="utf-8")

    def _model_ladder(self) -> str:
        src = self._inference_src()
        m = re.search(r"MODEL_LADDER\s*:\s*[^=]+=\s*\{", src)
        assert m, "MODEL_LADDER not found in kernel inference module"
        return src[m.start():]

    def test_model_ladder_does_not_mention_hermes_cli(self):
        src = self._inference_src()
        # The kernel plane must never route through ProviderRouter/hermes_cli;
        # that is the pipeline plane's job (config/providers.yaml).
        assert "hermes_cli" not in src or re.search(
            r"hermes_cli", src.split("MODEL_LADDER")[0]
        ), (
            "kernel MODEL_LADDER must not include hermes_cli/ProviderRouter tiers; "
            "the two planes stay separate (see tests/test_inference_planes.py)"
        )
        ladder_src = self._model_ladder()
        for tier in ("ProviderRouter", "providers.yaml", "task_class"):
            assert tier not in ladder_src, (
                f"kernel MODEL_LADDER references {tier!r} — planes must stay separate"
            )

    def test_ladder_entries_are_plain_local_or_kernel_models(self):
        ladder_src = self._model_ladder()
        entries = set(re.findall(r"[\"'](?:model[\"']\s*:\s*[\"'])([^\"']+)[\"']", ladder_src))
        entries |= set(re.findall(r"\"model\": ([A-Z_]+)", ladder_src))
        assert entries, "MODEL_LADDER has no model entries"
        banned = {"ox-alpha", "stealth/ox-alpha", "nous/stealth/ox-alpha",
                  "openrouter_ox", "frontier"}
        leaked = {e for e in entries if e in banned}
        assert not leaked, (
            f"MODEL_LADDER must not name pipeline-plane providers; found {leaked}"
        )


# ---------------------------------------------------------------------------
# 10. sensitive_context_stays_local remains true
# ---------------------------------------------------------------------------


class TestSensitiveContextStaysLocal:
    def test_flag_true(self, routing):
        escalation = routing.get("escalation") or {}
        assert escalation.get("sensitive_context_stays_local") is True, (
            "escalation.sensitive_context_stays_local must remain true: "
            "sensitive context never escalates to hosted endpoints"
        )
