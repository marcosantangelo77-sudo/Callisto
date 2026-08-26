"""Pin: ProviderRouter HTTP pool/payload/health live in tools.infrouter.http.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. Does NOT point MODEL_LADDER at
ProviderRouter. complete() and candidates_for() stay on the facade so
CALLISTO_LOCAL_ONLY strip + hermes_cli last-resort remain AST-pinned.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACADE = ROOT / "inference_router.py"
HTTP = ROOT / "tools" / "infrouter" / "http.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"

MOVED_FUNCS = (
    "shared_client",
    "reset_shared_client",
    "aclose_client",
    "build_payload",
    "post",
    "check_health",
    "health_report",
)


def _top_level_func_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _class_methods(path: Path, class_name: str) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    return {
        n.name: n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _imports_autonomous(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if "autonomous" in a.name:
                    return True
        elif isinstance(node, ast.ImportFrom):
            if "autonomous" in (node.module or ""):
                return True
    return False


def test_http_helpers_live_in_infrouter_http():
    names = _top_level_func_names(HTTP)
    for name in MOVED_FUNCS:
        assert name in names, name
    facade_top = _top_level_func_names(FACADE)
    for name in MOVED_FUNCS:
        assert name not in facade_top, name


def test_facade_keeps_thin_wrappers():
    methods = _class_methods(FACADE, "ProviderRouter")
    assert "complete" in methods
    assert "candidates_for" in methods
    assert "tier_for" in methods
    dump_post = ast.dump(methods["_post"])
    assert "_http" in dump_post or "post" in dump_post
    dump_health = ast.dump(methods["check_health"])
    assert "check_health" in dump_health

    def _body_has_async_client_ctor(fn) -> bool:
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "AsyncClient":
                    return True
        return False

    # Wrappers must not construct the pool themselves.
    assert not _body_has_async_client_ctor(methods["_post"])
    assert not _body_has_async_client_ctor(methods["check_health"])
    assert not _body_has_async_client_ctor(methods["_shared_client"])


def test_complete_and_candidates_for_stay_in_facade():
    methods = _class_methods(FACADE, "ProviderRouter")
    complete = methods["complete"]
    cands = methods["candidates_for"]
    dump_c = ast.dump(complete)
    dump_f = ast.dump(cands)
    assert "candidates_for" in dump_c
    assert "hermes_cli" in dump_c
    assert "strip_hosted_for_local_only" in dump_f
    strip_pos = dump_f.find("strip_hosted_for_local_only")
    avail_pos = dump_f.find("available")
    assert strip_pos != -1 and avail_pos != -1
    assert strip_pos < avail_pos


def test_facade_line_count_dropped():
    n = FACADE.read_text(encoding="utf-8").count("\n")
    assert n < 500, n
    http_n = HTTP.read_text(encoding="utf-8").count("\n")
    assert http_n >= 120, http_n


def test_http_module_does_not_import_autonomous_or_facade():
    assert not _imports_autonomous(HTTP)
    assert not _imports_autonomous(FACADE)
    tree = ast.parse(HTTP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "inference_router"
        elif isinstance(node, ast.Import):
            for a in node.names:
                assert a.name != "inference_router"


def test_http_does_not_name_model_ladder():
    tree = ast.parse(HTTP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "MODEL_LADDER"
        elif isinstance(node, ast.Attribute):
            assert node.attr != "MODEL_LADDER"


def test_paper_signal_still_paper_trading_only():
    src = PAPER.read_text(encoding="utf-8")
    assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src
    assigned = None
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES":
                    assigned = node.value
    assert assigned is not None
    dump = ast.dump(assigned)
    assert "paper_trading" in dump
    assert "live" not in dump


def test_runtime_wrappers_delegate_same_objects():
    from inference_router import ProviderRouter
    from tools.infrouter import http as http_mod

    r = ProviderRouter()
    assert r._shared_client is not None
    # Method objects wrap the module functions; calling reset uses the helper.
    r._http_client = object()
    r._reset_shared_client()
    assert r._http_client is None
    payload = r._payload(
        next(iter(r.endpoints.values())),
        [{"role": "user", "content": "hi"}],
        None, None, 16,
    )
    assert payload["max_tokens"] == 16
    assert payload["model"]
    assert http_mod.build_payload is not None
