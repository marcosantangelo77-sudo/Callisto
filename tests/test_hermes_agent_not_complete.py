"""Pin Hermes as the AGENT RUNTIME, not a completion transport.

Callisto's architecture separates two roles that must never be conflated:

1. **Hermes = agent runtime.** ``scripts/nous-supervisor.sh`` launches
   ``hermes`` as an interactive OX worker with ``--provider <p> -m
   stealth/ox-alpha``. That is process supervision of an autonomous
   coding agent — NOT an LLM completion call in the serving path.

2. **Completions stay HTTP.** The kernel plane (MODEL_LADDER +
   ``inference.complete()``) walks ordered model lists and talks HTTP
   (llama.cpp server, OpenAI-compatible endpoints, OpenRouter). The CLI/
   pipeline plane (ProviderRouter + config/providers.yaml) also serves
   completions over HTTP for its gpu1/openrouter_ox/frontier tiers;
   its ``hermes_cli`` backend exists only as a last-resort failover tier,
   never for gpu1/openrouter_ox/frontier.

Measured fresh-fork latency (findings/hermes_latency_2026-08-26.md,
p50 ≈ 11.9s / max ≈ 31.4s) forbids pointing MODEL_LADDER at
ProviderRouter or at any hermes subprocess path. These tests fail if:

* MODEL_LADDER or ``inference.complete()`` shells out to hermes /
  hermes_cli / subprocess hermes;
* config/providers.yaml gives gpu1, openrouter_ox, or frontier a
  ``backend: hermes_cli``;
* scripts/nous-supervisor.sh stops launching hermes as the OX agent
  with an explicit ``--provider`` and ``-m stealth/ox-alpha``;
* anyone wires MODEL_LADDER to ProviderRouter (AST + source scan).

Tests only — no product code is modified by this module.
"""

import ast
import inspect
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_PATH = REPO_ROOT / "inference_kernel.py"
ROUTER_PATH = REPO_ROOT / "inference_router.py"
SUPERVISOR_PATH = REPO_ROOT / "scripts" / "nous-supervisor.sh"
PROVIDERS_YAML_PATH = REPO_ROOT / "config" / "providers.yaml"

# Tiers whose completions MUST remain HTTP-backed (never hermes_cli).
HTTP_ONLY_TIERS = ("gpu1", "gpu1_fast", "frontier", "openrouter_ox",
                   "ox_alpha_proxy")

# Subprocess / CLI markers that would mean the kernel plane forks Hermes.
HERMES_FORK_MARKERS = (
    "hermes_cli",
    "hermes_complete",
    "subprocess",
    "Popen",
    "check_output",
    "run(",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _parse(p: Path) -> ast.Module:
    return ast.parse(_read(p), filename=str(p))


# ---------------------------------------------------------------------------
# 1. Kernel plane: MODEL_LADDER / complete() do NOT fork Hermes
# ---------------------------------------------------------------------------

class TestKernelPlaneDoesNotForkHermes:
    """MODEL_LADDER and inference.complete() stay on the HTTP ladder walk."""

    def test_kernel_module_exists(self):
        assert KERNEL_PATH.is_file(), f"{KERNEL_PATH} missing"

    def test_model_ladder_exists_and_nonempty(self):
        import inference

        assert isinstance(inference.MODEL_LADDER, dict)
        assert len(inference.MODEL_LADDER) >= 3

    def test_model_ladder_values_are_model_name_dicts(self):
        """Every rung is a plain {model, quality, timeout} dict — not an
        endpoint-pool reference or ProviderRouter pointer."""
        import inference

        for task, rungs in inference.MODEL_LADDER.items():
            assert isinstance(rungs, list) and rungs, task
            for rung in rungs:
                assert isinstance(rung, dict), (task, rung)
                assert "model" in rung, (task, rung)
                model = rung["model"]
                assert isinstance(model, str), (task, rung)
                # A ProviderRouter wiring would leak endpoint names here.
                assert not model.startswith(("provider:", "endpoint:",
                                             "router:")), \
                    f"MODEL_LADDER[{task}] points at ProviderRouter: {model}"

    def test_model_ladder_source_has_no_hermes_markers(self):
        src = _read(KERNEL_PATH)
        m = re.search(r"^MODEL_LADDER:[^=]*=\s*\{", src,
                      re.MULTILINE)
        assert m, "MODEL_LADDER assignment not found in inference_kernel.py"
        depth = 0
        end = None
        for i in range(m.end() - 1, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        assert end, "could not find MODEL_LADDER closing brace"
        ladder_src = src[m.start():end]
        assert "hermes" not in ladder_src.lower(), \
            "MODEL_LADDER mentions hermes"
        assert "ProviderRouter" not in ladder_src, \
            "MODEL_LADDER mentions ProviderRouter"
        assert "providers.yaml" not in ladder_src, \
            "MODEL_LADDER references providers.yaml"

    def test_ladder_walk_entrypoint_does_not_reference_provider_router(self):
        """The kernel walk is escalate_with_ladder() (post-split, there is
        no inference.complete(); the ladder IS the completion path)."""
        import inference

        for name in ("complete", "escalate_with_ladder"):
            obj = getattr(inference, name, None)
            if name == "complete":
                # complete() must NOT exist — its removal was deliberate;
                # if it comes back it must stay router-free (checked below).
                assert not callable(obj) or "ProviderRouter" not in \
                    inspect.getsource(obj)
                continue
            assert callable(obj), f"inference.{name} missing"
            src = inspect.getsource(obj)
            for marker in ("ProviderRouter", "hermes_cli", "hermes_complete",
                           "load_providers_config"):
                assert marker not in src, \
                    f"inference.{name} references {marker}"

    def test_kernel_module_ast_has_no_subprocess_import(self):
        tree = _parse(KERNEL_PATH)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name for a in node.names}
                banned = names & {"subprocess", "sh", "pty", "expect"}
                assert not banned, f"inference_kernel imports {banned}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod.split(".")[0] not in {"subprocess", "sh"}, \
                    f"inference_kernel imports from {mod}"

    def test_no_hermes_subprocess_call_in_kernel_ast(self):
        tree = _parse(KERNEL_PATH)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", getattr(fn, "attr", ""))
                assert name not in {"Popen", "run", "check_output",
                                    "check_call", "call"}, \
                    f"suspicious subprocess-style call {name!r} at line " \
                    f"{node.lineno} of inference_kernel.py"

    def test_router_hermes_cli_is_quarantined_to_failover_tier(self):
        """inference_router.py MAY mention hermes_cli (it implements the
        ox_alpha last-resort tier) but the kernel must not import it."""
        router_src = _read(ROUTER_PATH)
        assert "hermes_cli" in router_src  # quarantined implementation lives here
        kernel_src = _read(KERNEL_PATH)
        assert "from inference_router import" not in kernel_src
        assert "import inference_router" not in kernel_src


# ---------------------------------------------------------------------------
# 2. providers.yaml: gpu1 / openrouter_ox / frontier are NOT hermes_cli
# ---------------------------------------------------------------------------

class TestProvidersYamlBackends:
    @staticmethod
    def _cfg():
        return yaml.safe_load(_read(PROVIDERS_YAML_PATH))

    def test_providers_yaml_loads(self):
        cfg = self._cfg()
        assert isinstance(cfg.get("providers"), dict)

    def test_http_only_tiers_are_not_hermes_cli(self):
        cfg = self._cfg()
        for tier in HTTP_ONLY_TIERS:
            ep = cfg["providers"].get(tier)
            assert ep is not None, f"tier {tier} missing from providers.yaml"
            backend = ep.get("backend")
            assert backend != "hermes_cli", (
                f"tier {tier} has backend=hermes_cli; completions for this "
                f"tier must stay HTTP (llama_cpp_server/openai_compat)"
            )

    def test_gpu1_backend_is_llama_cpp_server(self):
        assert self._cfg()["providers"]["gpu1"]["backend"] == \
            "llama_cpp_server"

    def test_openrouter_ox_backend_is_openai_compat_over_http(self):
        ep = self._cfg()["providers"]["openrouter_ox"]
        assert ep["backend"] == "openai_compat"
        base = ep.get("base_url") or ""
        assert base.startswith("http"), "openrouter_ox lost its HTTP base_url"
        assert "openrouter.ai" in base

    def test_frontier_backend_is_openai_compat(self):
        assert self._cfg()["providers"]["frontier"]["backend"] == \
            "openai_compat"

    def test_only_ox_alpha_may_use_hermes_cli_backend(self):
        cfg = self._cfg()
        cli_tiers = [name for name, ep in cfg["providers"].items()
                     if ep.get("backend") == "hermes_cli"]
        assert set(cli_tiers) <= {"ox_alpha"}, \
            f"hermes_cli leaked into non-failover tiers: {cli_tiers}"

    def test_routing_ladders_prefer_http_before_any_cli_tier(self):
        """In every routing list, any hermes_cli tier comes AFTER at least
        one HTTP tier — the CLI fork is last-resort only."""
        cfg = self._cfg()
        routing = cfg.get("routing") or {}
        task_classes = routing.get("task_classes") or {}
        ladders = {k: v for k, v in task_classes.items()
                   if isinstance(v, list)}
        assert ladders, "routing ladders vanished from providers.yaml"
        for task, order in ladders.items():
            http_pos = [i for i, t in enumerate(order)
                        if t in cfg["providers"]
                        and cfg["providers"][t].get("backend")
                        != "hermes_cli"]
            assert http_pos, f"{task}: no HTTP tier before CLI fallback"
            first_http = min(http_pos)
            for i, t in enumerate(order):
                if t in cfg["providers"] and \
                        cfg["providers"][t].get("backend") == "hermes_cli":
                    assert i > first_http, \
                        f"{task}: hermes_cli tier {t} precedes an HTTP tier"


# ---------------------------------------------------------------------------
# 3. Supervisor: hermes is launched as the OX agent runtime
# ---------------------------------------------------------------------------

class TestSupervisorLaunchesHermesAgent:
    def test_supervisor_script_exists(self):
        assert SUPERVISOR_PATH.is_file()

    def test_invokes_hermes_binary_as_agent(self):
        src = _read(SUPERVISOR_PATH)
        # The actual launch line is the bare `hermes \` command continuation.
        assert re.search(r"(?m)^hermes\s*\\$", src), \
            "supervisor does not launch the hermes binary directly"

    def test_launch_line_passes_explicit_provider_flag(self):
        src = _read(SUPERVISOR_PATH)
        launch_start = re.search(r"(?m)^hermes\s*\\$", src)
        assert launch_start, "no hermes launch block found"
        block = src[launch_start.start():]
        # Take the command up to the terminating non-continuation line.
        lines = []
        for line in block.splitlines():
            lines.append(line)
            if not line.rstrip().endswith("\\"):
                break
        argv_block = "\n".join(lines)
        assert "--provider \"$PROVIDER\"" in argv_block, \
            "hermes launch lacks --provider flag"

    def test_default_model_is_stealth_ox_alpha(self):
        src = _read(SUPERVISOR_PATH)
        assert re.search(
            r'MODEL="\$\{CALLISTO_HERMES_MODEL:-stealth/ox-alpha\}"', src
        ), "supervisor default model is no longer stealth/ox-alpha"
        assert '-m "$MODEL"' in src, "supervisor does not pass -m $MODEL"

    def test_provider_prefers_openrouter_when_key_present(self):
        src = _read(SUPERVISOR_PATH)
        m = re.search(
            r'if \[\[ -n "\$\{OPENROUTER_API_KEY:-\}" \]\]; then\s*\n'
            r'\s*PROVIDER="openrouter"', src)
        assert m, "openrouter-first default provider selection removed"

    def test_nou_portal_never_the_unconditional_default(self):
        """nous may be the fallback when no OpenRouter key exists, but it
        must not be hardcoded as the always-on provider."""
        src = _read(SUPERVISOR_PATH)
        assert 'PROVIDER="nous"' in src
        # The nous assignment must be inside the else-branch after the
        # OPENROUTER_API_KEY check.
        idx_or_check = src.find('OPENROUTER_API_KEY')
        idx_nous = src.find('PROVIDER="nous"')
        assert 0 < idx_or_check < idx_nous, \
            "nous default precedes the OpenRouter-key check"

    def test_model_env_override_stays_ox_alpha_family(self):
        """Even when CALLISTO_HERMES_MODEL overrides, the documented
        contract comment still names stealth/ox-alpha."""
        src = _read(SUPERVISOR_PATH)
        assert "stealth/ox-alpha" in src

    def test_supervisor_refuses_master_branch(self):
        src = _read(SUPERVISOR_PATH)
        assert '"$branch" != "master"' in src, \
            "supervisor lost its master-worktree guard"


# ---------------------------------------------------------------------------
# 4. Guard: nobody points MODEL_LADDER at ProviderRouter
# ---------------------------------------------------------------------------

class TestModelLadderNotWiredToProviderRouter:
    """AST + source scans that fail if the kernel plane is unified onto
    ProviderRouter without a deliberate migration."""

    def test_kernel_does_not_import_router_symbols(self):
        tree = _parse(KERNEL_PATH)
        banned = {"ProviderRouter", "load_providers_config",
                  "EndpointConfig", "route_completion"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add((a.asname or a.name).split(".")[0])
        leaked = imported & banned
        assert not leaked, \
            f"inference_kernel imported ProviderRouter symbols: {leaked}"

    def test_kernel_calls_do_not_invoke_router(self):
        tree = _parse(KERNEL_PATH)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and \
                        fn.attr in {"complete", "complete_sync",
                                    "pick_endpoint"}:
                    base = fn.value
                    if isinstance(base, ast.Name) and \
                            base.id in {"router", "ProviderRouter",
                                        "providers"}:
                        raise AssertionError(
                            f"kernel calls router.{fn.attr}() at line "
                            f"{node.lineno}")

    def test_reexports_keep_ladder_canonical_and_walk_present(self):
        import inference
        assert hasattr(inference, "MODEL_LADDER")
        assert hasattr(inference, "escalate_with_ladder")

    def test_latency_finding_document_referenced_by_tests(self):
        finding = REPO_ROOT / "findings" / "hermes_latency_2026-08-26.md"
        if finding.is_file():
            text = finding.read_text(encoding="utf-8")
            assert "p50" in text or "11.9" in text, \
                "latency finding lost its measurements"

    def test_router_docstring_still_warns_against_unification(self):
        src = _read(ROUTER_PATH)
        doc = ast.get_docstring(ast.parse(src)) or ""
        assert "Do NOT unify" in doc or "do not unify" in doc.lower(), \
            "inference_router docstring dropped the two-plane warning"

    def test_kernel_comment_still_pins_two_plane_coexistence(self):
        src = _read(KERNEL_PATH)
        assert "TWO INFERENCE PLANES" in src, \
            "kernel lost its two-plane pinning comment"

    def test_no_test_widens_live_status_via_this_path(self):
        """Sanity guard tied to the fleet contract: nothing in this repo's
        inference plane should ever see a literal 'live' paper-trade status
        widening through the kernel or router."""
        for p in (KERNEL_PATH, ROUTER_PATH):
            src = _read(p)
            assert '"live"' not in src and "'live'" not in src, \
                f"{p.name} contains a literal 'live' status"
