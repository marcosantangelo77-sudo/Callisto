"""Extraction of ResearchLoop._phase_* implementations to tools.loop.phases_impl.

Contract:
- Every ``_phase_*`` method stays on ResearchLoop as a wrapper delegating to
  a ``phase_*`` function in tools/loop/phases_impl.py taking the loop instance.
- The sequencer tables (PHASES / PERIODIC_PHASES) still drive execution via
  method names — nothing about ordering or gating changed.
- live_execute remains env-gated (CALLISTO_ALLOW_LIVE_EXECUTE) before any
  list_hypotheses call.
- autonomous.py must not import hung paths; phases_impl must never import
  tools.autonomous (no cycles).
"""

import ast
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub polars before importing tools.autonomous.
if "polars" not in sys.modules:
    try:
        import polars  # noqa: F401
    except ModuleNotFoundError:
        _pl = types.ModuleType("polars")
        _pl.DataFrame = type("DataFrame", (), {})
        _pl.Series = object
        _pl.read_parquet = lambda *a, **k: None
        sys.modules["polars"] = _pl

import tools.autonomous as auto
from tools.loop import phases_impl


def _read(path):
    with open(path) as f:
        return f.read()


class TestImplFunctionsExist:
    def test_every_phase_method_has_impl(self):
        for attr in dir(auto.ResearchLoop):
            if attr.startswith("_phase_"):
                impl = attr.replace("_phase_", "phase_", 1)
                assert hasattr(phases_impl, impl), f"phases_impl missing {impl}"

    def test_impls_are_coroutines_taking_loop(self):
        for attr in dir(auto.ResearchLoop):
            if not attr.startswith("_phase_"):
                continue
            fn = getattr(phases_impl, attr.replace("_phase_", "phase_", 1))
            assert ast.parse(f"async def f(): pass")  # sanity
            import asyncio
            assert asyncio.iscoroutinefunction(fn), attr
            code = fn.__code__
            assert code.co_varnames[0] == "loop", attr

    def test_wrappers_delegate(self):
        src = _read(auto.__file__)
        tree = ast.parse(src)
        rl = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ResearchLoop")
        wrapped = 0
        for m in rl.body:
            if isinstance(m, ast.AsyncFunctionDef) and m.name.startswith("_phase_"):
                body = [n for n in m.body if not isinstance(n, (ast.Expr, ast.Import, ast.If))]
                assert len(body) == 1 and isinstance(body[0], ast.Return), m.name
                call = body[0].value
                if isinstance(call, ast.Await):
                    call = call.value
                assert getattr(call.func, "attr", "").startswith("phase_"), m.name
                assert call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "self"
                wrapped += 1
        assert wrapped >= 20


class TestLiveExecuteGate:
    def test_gate_before_list_hypotheses_in_wrapper_and_impl(self):
        for path in (auto.__file__, phases_impl.__file__):
            src = _read(path)
            i = src.index("async def _phase_live_execute" if "autonomous" in path
                          else "async def phase_live_execute")
            j = src.find("\n    async def ", i + 10)
            if j == -1:
                j = src.find("\nasync def ", i + 10)
            body = src[i:j]
            gate = body.index('getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"')
            hyp = body.find("list_hypotheses")
            assert hyp == -1 or gate < hyp, path

    def test_impl_gates_at_runtime(self):
        import asyncio

        class _Loop:
            hypothesis_manager = None

        # Gate closed (default): returns without touching hypothesis_manager.
        res = asyncio.run(phases_impl.phase_live_execute(_Loop()))
        assert res is None
        assert _Loop.hypothesis_manager is None


class TestNoCircularImport:
    def test_phases_impl_never_imports_autonomous(self):
        tree = ast.parse(_read(phases_impl.__file__))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert "autonomous" not in a.name
            elif isinstance(node, ast.ImportFrom):
                assert "autonomous" not in (node.module or "")

    def test_sequencer_methods_still_resolve(self):
        from tools.loop import sequencer

        for spec in (*sequencer.PHASES, *sequencer.PERIODIC_PHASES):
            assert hasattr(auto.ResearchLoop, spec.method), spec.method


class TestSharedStateReexported:
    def test_constants_importable_from_autonomous(self):
        for name in ("RESEARCH_SPORTS", "_regime_cache", "_wiki_in_loop_enabled",
                     "_fetch_wiki_priors", "CLAUDE_ESCALATION_COOLDOWN",
                     "MIN_EDGE_THRESHOLD_FLOOR"):
            assert hasattr(auto, name), name

    def test_regime_cache_shared_object(self):
        assert auto._regime_cache is phases_impl._regime_cache

    def test_api_endpoint_import_works(self):
        ns = {}
        exec("from tools.autonomous import RESEARCH_SPORTS", ns)
        assert isinstance(ns["RESEARCH_SPORTS"], list)


class TestLineCountDrop:
    def test_autonomous_shrunk_by_hundreds(self):
        lines = _read(auto.__file__).count("\n")
        assert lines < 4000, lines
