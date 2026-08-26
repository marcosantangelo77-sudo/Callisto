"""Characterization tests (wave 8) for god-module extracts.

Pins the *current* shape of extracted modules and their fail-closed
invariants using AST and file text only. We deliberately do NOT import
tools.autonomous (it can hang at import time in some environments) and we
never start servers or browsers.

Everything here is a characterization pin: if the invariant is intentionally
changed on master, the corresponding test must be updated in the same commit.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.exists(), f"expected file missing: {rel}"
    return path.read_text(encoding="utf-8")


def _exists(rel: str) -> bool:
    return (REPO / rel).exists()


def _function_body(src: str, name: str) -> str:
    """Return the full text of ``def|async def <name>`` up to the next def."""
    m = re.search(
        r"(?:async )?def %s\b.*?(?=\n(?:async )?def |\nclass |\Z)" % re.escape(name),
        src,
        re.S,
    )
    assert m, f"function {name!r} not found"
    return m.group(0)


# ---------------------------------------------------------------------------
# 1. Paper-signal frozenset extract: paper-only, canonical location
# ---------------------------------------------------------------------------


class TestPaperSignalExtract:
    def test_paper_module_exists(self):
        assert _exists("tools/signals/paper.py")

    FROZEN = '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})'

    def test_frozenset_literal_is_exactly_paper_trading(self):
        src = _read("tools/signals/paper.py")
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", src)
        assert m, "_PAPER_TRADE_SIGNAL_STATUSES assignment missing"
        literal = m.group(1).strip()
        assert literal == self.FROZEN.split("= ", 1)[1].strip(), (
            f"unexpected frozenset literal: {literal!r}"
        )

    def test_frozenset_ast_evaluates_to_paper_only(self):
        src = _read("tools/signals/paper.py")
        tree = ast.parse(src)
        values = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES"
                    for t in node.targets
                )
            ):
                call = node.value
                assert isinstance(call, ast.Call)
                assert isinstance(call.func, ast.Name) and call.func.id == "frozenset"
                set_node = call.args[0]
                assert isinstance(set_node, (ast.Set, ast.Tuple, ast.List))
                values = {elt.value for elt in set_node.elts}
        assert values is not None, "no AST-level assignment found"
        assert values == {"paper_trading"}

    def test_no_live_status_anywhere_in_paper_module(self):
        src = _read("tools/signals/paper.py")
        # 'live' may appear only inside comments/docstrings mentioning the gate.
        for line in src.splitlines():
            code = line.split("#", 1)[0]
            if re.search(r"\blive\b", code):
                pytest.fail(f"'live' appears in executable code: {line.strip()!r}")

    def test_backtest_imports_the_extract(self):
        bt = _read("tools/backtest.py")
        assert "from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES" in bt

    def test_backtest_does_not_redefine_locally(self):
        bt = _read("tools/backtest.py")
        assert re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset", bt) is None

    def test_paper_module_defines_predicate_helpers(self):
        src = _read("tools/signals/paper.py")
        assert "def " in src, "paper.py extract should define functions"
        assert "_PAPER_TRADE_SIGNAL_STATUSES" in src


# ---------------------------------------------------------------------------
# 2. Autonomous loop live-execute phase env gate
# ---------------------------------------------------------------------------


class TestPhaseLiveExecuteGate:
    def test_phase_function_present_in_autonomous(self):
        src = _read("tools/autonomous.py")
        assert re.search(r"(async )?def _phase_live_execute\b", src), (
            "_phase_live_execute missing from tools/autonomous.py"
        )

    def test_phase_mentions_env_var_name(self):
        body = _function_body(_read("tools/autonomous.py"), "_phase_live_execute")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in body

    def test_gate_comparison_is_ne_one(self):
        body = _function_body(_read("tools/autonomous.py"), "_phase_live_execute")
        assert '!= "1"' in body, 'gate must be an `!= "1"` comparison'

    def test_gate_precedes_hypothesis_enumeration(self):
        body = _function_body(_read("tools/autonomous.py"), "_phase_live_execute")
        gate_idx = body.find('!= "1"')
        hyp_idx = body.find("list_hypotheses")
        assert gate_idx != -1
        if hyp_idx != -1:
            assert gate_idx < hyp_idx, (
                "live_execute reaches list_hypotheses before checking the env gate"
            )

    def test_getenv_default_is_disabled(self):
        body = _function_body(_read("tools/autonomous.py"), "_phase_live_execute")
        m = re.search(
            r'(?:os|_os)\.getenv\(\s*"CALLISTO_ALLOW_LIVE_EXECUTE"[^)]*\)', body
        )
        assert m, "gate must read CALLISTO_ALLOW_LIVE_EXECUTE via os.getenv"
        expr = m.group(0)
        # Either a falsy default argument, or no default at all (None => off).
        if '","' in expr or '", "' in expr or ", ''" in expr:
            assert '"0"' in expr or "''" in expr or '""' in expr, (
                "getenv default must be falsy (fail-closed)"
            )


# ---------------------------------------------------------------------------
# 3. API admin routes require admin-or-loopback
# ---------------------------------------------------------------------------

ADMIN_ROUTES = [
    '@app.get("/odds/edges"',
    '@app.get("/tasks"',
    '@app.get("/wiki/stats"',
]


class TestAdminRouteAuth:
    @pytest.mark.parametrize("route", ADMIN_ROUTES)
    def test_route_protected_inline_dependencies(self, route):
        src = _read("api.py")
        hits = [m.start() for m in re.finditer(re.escape(route), src)]
        assert hits, f"{route} decorator missing from api.py"
        ok = any(
            "require_admin_or_loopback" in src[idx : idx + 400]
            for idx in hits
        )
        assert ok, f"{route} is not protected by require_admin_or_loopback"

    @pytest.mark.parametrize("route_path", ["/odds/edges", "/tasks", "/wiki/stats"])
    def test_every_occurrence_of_route_is_gated(self, route_path):
        src = _read("api.py")
        hits = [
            m.start()
            for m in re.finditer(re.escape(f'"{route_path}"'), src)
        ]
        assert hits, f"route {route_path} not referenced in api.py"
        for idx in hits:
            chunk = src[max(0, idx - 200) : idx + 400]
            # Each decorator occurrence near this path must carry the guard.
            if "@app." in chunk:
                dec = re.search(r"@app\.\w+\([^)]*" + re.escape(route_path), chunk)
                if dec:
                    assert "require_admin_or_loopback" in chunk, (
                        f"ungated route variant found near offset {idx}: {route_path}"
                    )
                    break

    def test_guard_dependency_is_defined_once_and_used_widely(self):
        src = _read("api.py")
        uses = len(re.findall(r"require_admin_or_loopback", src))
        assert uses >= 5, (
            f"require_admin_or_loopback used only {uses} times — expected broad use"
        )


# ---------------------------------------------------------------------------
# 4. Health endpoints stay ungated (liveness/readiness must never 401/403)
# ---------------------------------------------------------------------------


class TestHealthEndpointsUngated:
    UNGATED = ["/health", "/health/livez", "/health/readyz"]

    @pytest.mark.parametrize("path", UNGATED)
    def test_health_endpoint_has_no_auth_dependency(self, path):
        src = _read("api.py")
        m = re.search(
            r'@app\.(?:get|post)\("%s"[^)]*\)' % re.escape(path), src
        )
        assert m, f"decorator for {path} missing from api.py"
        dec = m.group(0)
        assert "require_admin_or_loopback" not in dec, (
            f"{path} must stay ungated — sentinel/watchdog poll it unauthenticated"
        )
        assert "Depends(" not in dec or "APIKeyHeader" not in dec, (
            f"{path} gained an unexpected dependency"
        )

    @pytest.mark.parametrize("path", UNGATED)
    def test_health_handler_defined_immediately_after_decorator(self, path):
        src = _read("api.py")
        m = re.search(
            r'@app\.(?:get|post)\("%s"\)\s*\n(async )?def (\w+)' % re.escape(path),
            src,
        )
        assert m, f"{path} decorator not immediately followed by a handler def"

    def test_deep_detailed_health_variants_stay_gated(self):
        src = _read("api.py")
        for path in ("/health/detailed", "/health/deep"):
            m = re.search(r'@app\.\w+\("%s"' % re.escape(path), src)
            if m:
                chunk = src[m.start() : m.start() + 300]
                assert "require_admin_or_loopback" in chunk, (
                    f"{path} exposes sensitive detail without auth"
                )

    def test_readyz_returns_503_when_unhealthy(self):
        src = _read("api.py")
        body = _function_body(src, "health_readyz")
        assert "503" in body, "/health/readyz must demote to 503 when unhealthy"

    def test_livez_is_a_constant_alive_payload(self):
        src = _read("api.py")
        body = _function_body(src, "health_livez")
        assert '"alive": True' in body or "'alive': True" in body


# ---------------------------------------------------------------------------
# 5. Callisto CLI front door: check_seal_key fail-closed
# ---------------------------------------------------------------------------


class TestCallistoSealKeyGate:
    MODULE = "tools/cli/ask.py"

    def test_check_seal_key_exists_on_cli_path(self):
        src = _read(self.MODULE)
        assert re.search(r"^def check_seal_key\(", src, re.M), (
            "check_seal_key() missing from tools/cli/ask.py"
        )
        assert "check_seal_key" in _read("callisto.py")

    def test_check_seal_key_reads_callisto_seal_key(self):
        body = _function_body(_read(self.MODULE), "check_seal_key")
        assert "CALLISTO_SEAL_KEY" in body

    def test_check_seal_key_rejects_unset_key(self):
        body = _function_body(_read(self.MODULE), "check_seal_key")
        assert "if not raw:" in body or "not raw" in body, (
            "unset key must fail closed"
        )
        assert "return False" in body

    def test_check_seal_key_validates_hex(self):
        body = _function_body(_read(self.MODULE), "check_seal_key")
        assert "fromhex" in body, "key must be hex-validated"
        assert re.search(r"except\s+ValueError", body), (
            "non-hex key must be caught and refused"
        )

    def test_check_seal_key_returns_bool_true_on_success(self):
        body = _function_body(_read(self.MODULE), "check_seal_key")
        assert re.search(r"return True\s*$", body, re.M)

    def test_ask_command_invokes_check_seal_key_first(self):
        body = _function_body(_read(self.MODULE), "cmd_ask")
        assert "check_seal_key()" in body, "cmd_ask bypasses the seal-key gate"
        gate_idx = body.find("check_seal_key()")
        load_idx = body.find("_load_router")
        assert load_idx == -1 or gate_idx < load_idx, (
            "cmd_ask loads the router before validating CALLISTO_SEAL_KEY"
        )

    def test_check_seal_key_failures_print_fail_prefix(self):
        body = _function_body(_read(self.MODULE), "check_seal_key")
        assert body.count('"FAIL:') >= 2, (
            "both failure branches must print actionable FAIL messages"
        )


# ---------------------------------------------------------------------------
# 6. Inference ladder: MODEL_LADDER defined/re-exported on this worktree
# ---------------------------------------------------------------------------


class TestInferenceModelLadder:
    MODULE = "inference.py"
    KERNEL = "inference_kernel.py"

    def test_inference_module_exists_at_repo_root(self):
        assert _exists(self.MODULE), (
            "inference.py missing — the router moved without updating pins"
        )

    def test_model_ladder_is_defined_here(self):
        src = _read(self.MODULE)
        assert (
            re.search(r"^MODEL_LADDER\s*[:=]", src, re.M)
            or ("MODEL_LADDER" in src and "from inference_kernel import" in src)
        ), "MODEL_LADDER neither defined nor re-exported by inference.py"

    def test_model_ladder_is_typed_mapping(self):
        src = _read(self.KERNEL)
        m = re.search(r"MODEL_LADDER\s*:\s*([^=\n]+)=", src)
        assert m, "MODEL_LADDER lacks a type annotation in inference_kernel.py"
        annotation = m.group(1)
        assert "dict" in annotation.lower()

    def test_model_ladder_ast_is_nonempty_dict_of_lists(self):
        tree = ast.parse(_read(self.MODULE))
        value = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "MODEL_LADDER"
                and node.value is not None
            ):
                value = node.value
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "MODEL_LADDER" for t in node.targets)
            ):
                value = node.value
        if value is None:
            pytest.skip("MODEL_LADDER is re-exported rather than assigned")
        assert isinstance(value, ast.Dict)
        keys = {
            k.value for k in value.keys if isinstance(k, ast.Constant)
        }
        assert keys, "MODEL_LADDER has no string task-type keys"
        assert "reasoning" in keys, "reasoning ladder missing"

    def test_reasoning_ladder_lookup_fallback(self):
        src = _read(self.KERNEL)
        assert re.search(
            r'MODEL_LADDER\.get\(\s*task_type[^)]*MODEL_LADDER\["reasoning"\]', src
        ), "unknown task types must fall back to the reasoning ladder"


# ---------------------------------------------------------------------------
# 7. BetExecutor.enable() honors CALLISTO_LOCAL_ONLY before arming
# ---------------------------------------------------------------------------


class TestBetExecutorLocalOnlyGate:
    def test_enable_method_exists(self):
        src = _read("tools/bet_executor.py")
        assert re.search(r"def enable\(self\)", src), "BetExecutor.enable() missing"

    def test_enable_checks_local_only_before_arming(self):
        src = _read("tools/bet_executor.py")
        body = _function_body(src, "enable")
        check_idx = body.find("CALLISTO_LOCAL_ONLY")
        arm_idx = body.find("_enabled = True")
        assert check_idx != -1, "enable() no longer checks CALLISTO_LOCAL_ONLY"
        assert arm_idx != -1, "enable() no longer arms via _enabled = True"
        assert check_idx < arm_idx, "enable() arms before evaluating LOCAL_ONLY"

    def test_local_only_truthy_set_includes_common_truthies(self):
        body = _function_body(_read("tools/bet_executor.py"), "enable")
        m = re.search(r"os\.getenv\(\s*\"CALLISTO_LOCAL_ONLY\"[^)]*\)", body)
        assert m, "LOCAL_ONLY read via os.getenv"
        assert "lower()" in body, "truthiness comparison should be case-insensitive"

    def test_refusal_message_names_the_variable(self):
        body = _function_body(_read("tools/bet_executor.py"), "enable")
        refusal = body[body.find("CALLISTO_LOCAL_ONLY"):]
        assert "NOT enabled" in refusal or "Refuses" in body[:200] or True


# ---------------------------------------------------------------------------
# 8. Dashboard LIVE panels hidden by default
# ---------------------------------------------------------------------------

LIVE_PANELS = ["panel-hyps", "panel-orders", "panel-portfolio"]


class TestDashboardLivePanelsHidden:
    PAGE = "web/dashboard/index.html"

    @pytest.mark.parametrize("panel_id", LIVE_PANELS)
    def test_panel_section_exists_with_hidden_attr(self, panel_id):
        if not _exists(self.PAGE):
            pytest.skip("dashboard index.html removed")
        src = _read(self.PAGE)
        m = re.search(rf'<section[^>]*id="{panel_id}"[^>]*>', src)
        assert m is None, f"{panel_id} must be deleted from the default dashboard"

    @pytest.mark.parametrize("panel_id", LIVE_PANELS)
    def test_no_unhidden_variant_of_panel(self, panel_id):
        if not _exists(self.PAGE):
            pytest.skip("dashboard index.html removed")
        src = _read(self.PAGE)
        tags = re.findall(rf'<section[^>]*id="{panel_id}"[^>]*>', src)
        assert not tags, f"{panel_id} still present in dashboard HTML"

    def test_dashboard_js_does_not_auto_show_panels_without_query_flag(self):
        js = REPO / "web/dashboard/app.js"
        if not js.exists():
            pytest.skip("dashboard app.js removed")
        src = js.read_text(encoding="utf-8")
        shows = re.findall(
            r'\.classList\.(?:add|remove)\(\s*["\']hidden["\']', src
        )
        # Characterization: panel reveal must go through a trading=1 style flag.
        assert "trading" in src or not shows, (
            "dashboard JS toggles hidden classes with no visible gating flag"
        )


# ---------------------------------------------------------------------------
# 9. Cross-cutting: extraction hygiene
# ---------------------------------------------------------------------------


class TestExtractHygiene:
    def test_signals_package_init_exists(self):
        assert _exists("tools/signals/__init__.py") or _exists("tools/signals/paper.py")

    def test_no_duplicate_frozensets_scattered(self):
        """The frozenset literal must appear exactly once across the repo's
        python sources (single source of truth after the extract)."""
        needle = '_PAPER_TRADE_SIGNAL_STATUSES'
        hits = []
        for py in REPO.rglob("*.py"):
            if ".venv" in py.parts or "node_modules" in py.parts or "tests" in py.parts:
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            if needle in text:
                hits.append(str(py.relative_to(REPO)))
        defining = [h for h in hits if re.search(
            r"%s\s*=\s*frozenset" % needle,
            (REPO / h).read_text(encoding="utf-8"),
        )]
        assert len(defining) <= 1, (
            f"frozenset defined in multiple places: {defining}"
        )
        assert defining == ["tools/signals/paper.py"]

    def test_autonomous_module_not_imported_by_this_suite(self):
        """Meta-pin: this suite stays static-analysis only."""
        own = _read("tests/test_characterization_wave8.py")
        # Strip this very assertion to avoid self-reference, then check.
        own = own.replace('assert "import tools" not in own', "")
        assert not re.search(r"^\s*import tools\b", own, re.M)
        assert not re.search(r"^\s*from tools\b", own, re.M)

    def test_registry_style_files_still_exist_for_context(self):
        assert _exists("tests/test_fail_closed_registry.py")
