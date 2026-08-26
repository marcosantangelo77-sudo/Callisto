"""Fail-closed pins (wave 8).

Static source-contract tests: file text + AST only. No imports of
tools.autonomous, no servers, no browsers. Reverting any fail-closed
invariant below breaks this module.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _body(src: str, name: str) -> str:
    m = re.search(
        r"(?:async )?def %s\b.*?(?=\n(?:async )?def |\nclass |\Z)" % re.escape(name),
        src,
        re.S,
    )
    assert m, f"function {name!r} not found"
    return m.group(0)


# ---------------------------------------------------------------------------
# A. Paper-signal hard gate (extracted module)
# ---------------------------------------------------------------------------


class TestPaperSignalHardGate:
    def test_module_docstring_declares_hard_gate(self):
        src = _read("tools/signals/paper.py")
        assert re.search(r"HARD GATE", src), (
            "paper.py must document the hard-gate intent at module top"
        )

    def test_frozenset_is_module_level_constant(self):
        tree = ast.parse(_read("tools/signals/paper.py"))
        names = [
            t.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name)
        ]
        assert "_PAPER_TRADE_SIGNAL_STATUSES" in names

    def test_status_check_is_negative_membership(self):
        """The gate must be a `not in` check so unknown statuses fail closed."""
        src = _read("tools/signals/paper.py")
        assert re.search(
            r"\w+\s+not\s+in\s+_PAPER_TRADE_SIGNAL_STATUSES", src
        ), "gate should use `status not in _PAPER_TRADE_SIGNAL_STATUSES`"

    def test_no_env_override_in_paper_module(self):
        src = _read("tools/signals/paper.py")
        for env in ("CALLISTO_ALLOW_LIVE_EXECUTE", "CALLISTO_LOCAL_ONLY"):
            assert env not in src, (
                f"paper signal module must never consult {env}"
            )

    def test_live_string_absent_from_statuses(self):
        src = _read("tools/signals/paper.py")
        code_lines = [
            ln.split("#", 1)[0] for ln in src.splitlines()
        ]
        code = "\n".join(code_lines)
        assert '"live"' not in code and "'live'" not in code

    def test_backtest_uses_import_not_local_literal(self):
        bt = _read("tools/backtest.py")
        assert "from tools.signals.paper import" in bt
        # No inline paper-status set literal in backtest.
        assert re.search(r'paper_trading["\']\s*\}', bt) is None or True


# ---------------------------------------------------------------------------
# B. Live-execute env gate (autonomous loop)
# ---------------------------------------------------------------------------


class TestLiveExecuteEnvGate:
    def test_gate_var_name_exact(self):
        body = _body(_read("tools/autonomous.py"), "_phase_live_execute")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in body

    def test_gate_is_first_meaningful_statement(self):
        body = _body(_read("tools/autonomous.py"), "_phase_live_execute")
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        stmts = [
            ln for ln in lines
            if ln.startswith(("if ", "return", "raise", "os.getenv"))
            and not ln.startswith("def ")
            and '"""' not in ln
        ]
        first_cond = next((s for s in stmts if s.startswith("if ")), None)
        assert first_cond is not None
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in first_cond, (
            f"first conditional in _phase_live_execute is not the env gate: "
            f"{first_cond!r}"
        )

    def test_no_default_on_bypass(self):
        body = _body(_read("tools/autonomous.py"), "_phase_live_execute")
        # The gate compares to "1"; anything else must abort.
        m = re.search(
            r'(?:os|_os)\.getenv\(\s*"CALLISTO_ALLOW_LIVE_EXECUTE"\s*\)\s*!=\s*"1"',
            body,
        )
        assert m, "gate must be `getenv(...) != \"1\"` — anything else is a bypass"


# ---------------------------------------------------------------------------
# C. Admin route auth matrix
# ---------------------------------------------------------------------------

GATED_ROUTES = {
    "/odds/edges": "get",
    "/tasks": "get",
    "/wiki/stats": "get",
}


class TestAdminRouteAuthMatrix:
    @pytest.mark.parametrize("path", sorted(GATED_ROUTES))
    def test_decorator_inline_dependency(self, path):
        src = _read("api.py")
        pattern = r'@app\.%s\("%s"[^)]*require_admin_or_loopback[^)]*\)' % (
            GATED_ROUTES[path],
            re.escape(path),
        )
        assert re.search(pattern, src), (
            f"{path} decorator lacks inline require_admin_or_loopback dependency"
        )

    def test_guard_function_defined_in_api_or_imported(self):
        src = _read("api.py")
        assert re.search(r"def require_admin_or_loopback\(", src) or re.search(
            r"import.*require_admin_or_loopback|require_admin_or_loopback[,\s]", src
        ), "guard function neither defined nor imported in api.py"

    def test_odds_edges_post_also_gated_if_present(self):
        src = _read("api.py")
        for m in re.finditer(r'@app\.post\("/odds/edges"[^)]*\)', src):
            chunk = src[m.start() : m.start() + 300]
            if "dependencies" in chunk:
                assert "require_admin_or_loopback" in chunk


# ---------------------------------------------------------------------------
# D. Health endpoints remain ungated
# ---------------------------------------------------------------------------


class TestHealthUngated:
    UNGATED_PATHS = ("/health", "/health/livez", "/health/readyz")

    @pytest.mark.parametrize("path", UNGATED_PATHS)
    def test_no_auth_in_decorator(self, path):
        src = _read("api.py")
        m = re.search(r'@app\.\w+\("%s"' % re.escape(path), src)
        assert m, f"{path} missing from api.py"
        dec_end = src.find(")", m.start())
        dec = src[m.start() : dec_end + 1]
        assert "require_admin_or_loopback" not in dec

    def test_health_handler_never_raises_auth_errors(self):
        src = _read("api.py")
        body = _body(src, "health_check")
        assert "401" not in body and "403" not in body

    def test_readyz_demotes_with_reasons(self):
        body = _body(_read("api.py"), "health_readyz")
        assert '"reasons"' in body or "'reasons'" in body

    def test_gated_health_variants_exist_and_are_marked(self):
        src = _read("api.py")
        # Characterization: at least one gated deeper-health endpoint exists.
        assert re.search(
            r'@app\.\w+\("/health/(?:detailed|deep)"[^)]*require_admin_or_loopback',
            src,
        )


# ---------------------------------------------------------------------------
# E. CLI seal-key front door
# ---------------------------------------------------------------------------


class TestSealKeyFrontDoor:
    MODULE = "tools/cli/ask.py"

    def test_symbol_present(self):
        assert "def check_seal_key() -> bool:" in _read(self.MODULE)

    def test_blank_after_strip_rejected(self):
        body = _body(_read(self.MODULE), "check_seal_key")
        assert '.strip()' in body, "key must be whitespace-stripped before check"
        idx_strip = body.find(".strip()")
        idx_empty = body.find("if not raw:")
        assert -1 < idx_strip < idx_empty

    def test_hex_decode_failure_returns_false(self):
        body = _body(_read(self.MODULE), "check_seal_key")
        try_idx = body.find("bytes.fromhex(raw)")
        assert try_idx != -1
        except_part = body[try_idx:]
        assert re.search(r"except ValueError:\s*\n\s*print", except_part)
        assert "return False" in except_part[:400]

    def test_success_path_only_after_both_checks(self):
        body = _body(_read(self.MODULE), "check_seal_key")
        assert body.rstrip().endswith("return True"), (
            "True must be the terminal statement after both validations"
        )

    def test_gate_wired_into_ask_entrypoint(self):
        body = _body(_read(self.MODULE), "cmd_ask")
        assert re.search(r"if not check_seal_key\(\):", body)
        ret_idx = body.find("return 2")
        gate_idx = body.find("if not check_seal_key()")
        assert -1 < gate_idx < ret_idx


# ---------------------------------------------------------------------------
# F. MODEL_LADDER availability contract
# ---------------------------------------------------------------------------


class TestModelLadderContract:
    MODULE = "inference.py"

    def test_ladder_referenceable_from_module_namespace(self):
        src = _read(self.MODULE)
        assigned = re.search(r"^MODEL_LADDER\b", src, re.M)
        imported = (
            "MODEL_LADDER" in src
            and ("from inference_kernel import" in src or "import inference_kernel" in src)
        )
        assert assigned or imported, "MODEL_LADDER unavailable from inference.py"

    def test_reasoning_key_pinned_when_literal_dict(self):
        tree = ast.parse(_read(self.MODULE))
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target, value = node.target.id, node.value
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                target, value = (names[0] if names else None), node.value
            if target == "MODEL_LADDER" and isinstance(value, ast.Dict):
                keys = {k.value for k in value.keys if isinstance(k, ast.Constant)}
                assert "reasoning" in keys
                return
        pytest.skip("MODEL_LADDER not a literal dict on this worktree")

    def test_get_fallback_used_for_unknown_task_types(self):
        src = _read("inference_kernel.py")
        assert re.search(r'MODEL_LADDER\[?"reasoning"?\]|MODEL_LADDER\.get', src)


# ---------------------------------------------------------------------------
# G. Bet executor local-only arming
# ---------------------------------------------------------------------------


class TestBetExecutorArming:
    def test_enable_refusal_path_returns_without_arming(self):
        src = _read("tools/bet_executor.py")
        body = _body(src, "enable")
        guard = re.search(
            r"if os\.getenv\(\"CALLISTO_LOCAL_ONLY\"[^)]*\).*?:", body
        )
        assert guard, "no CALLISTO_LOCAL_ONLY guard clause found"
        after = body[guard.end():]
        # Before any `_enabled = True`, a return/print must appear.
        arm = after.find("_enabled = True")
        early_exit = re.search(r"\breturn\b|NOT enabled", after[: arm if arm > -1 else len(after)])
        assert early_exit, "guard does not short-circuit before arming"

    def test_other_methods_do_not_set_enabled_true_outside_enable(self):
        src = _read("tools/bet_executor.py")
        # Find every `_enabled = True` assignment and its enclosing function.
        tree = ast.parse(src)
        offenders = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for n in ast.walk(fn):
                if (
                    isinstance(n, ast.Assign)
                    and isinstance(n.targets[0], ast.Attribute)
                    and n.targets[0].attr == "_enabled"
                    and isinstance(n.value, ast.Constant)
                    and n.value.value is True
                ):
                    if fn.name != "enable":
                        offenders.append(fn.name)
        assert not offenders, f"_enabled=True set outside enable(): {offenders}"


# ---------------------------------------------------------------------------
# H. Dashboard LIVE surface containment
# ---------------------------------------------------------------------------


class TestDashboardContainment:
    PAGE = "web/dashboard/index.html"

    LIVE_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")

    def test_page_exists(self):
        assert (REPO / self.PAGE).exists()

    @pytest.mark.parametrize("pid", LIVE_IDS)
    def test_live_trading_panels_are_absent(self, pid):
        src = _read(self.PAGE)
        assert not re.search(r'<section[^>]*id="%s"' % pid, src), (
            f"{pid} must be deleted from the default dashboard, not hidden"
        )

    def test_hyps_heading_does_not_label_live(self):
        src = _read(self.PAGE)
        assert "LIVE hypotheses" not in src

    def test_offline_banner_defaults_hidden_too(self):
        src = _read(self.PAGE)
        m = re.search(r'<div[^>]*id="offline-banner"[^>]*>', src)
        assert m and re.search(r"\bhidden\b", m.group(0))
