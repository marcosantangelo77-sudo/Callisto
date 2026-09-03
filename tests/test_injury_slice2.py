"""Pin: NFL/MLB impact+redistribute helpers extracted to tools.injury.impact_nfl_mlb.

Does NOT import tools.autonomous (that module hangs this environment).
Does NOT arm live betting. Does NOT add live to paper-signal.
model.py keeps dataclasses, NBA tier/impact/redistribute, player_impact,
and redistribute_usage; those two dispatchers import the extracted helpers.
Extracted names are re-exported (ImportFrom or assignment).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "tools" / "injury" / "model.py"
IMPACT = ROOT / "tools" / "injury" / "impact_nfl_mlb.py"
ANALYSIS = ROOT / "tools" / "injury" / "analysis.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"

MOVED = (
    "_determine_nfl_backup_quality",
    "_determine_mlb_tier",
    "_nfl_player_impact",
    "_nfl_redistribute_targets",
    "_mlb_player_impact",
    "_nfl_usage_redistribution",
    "_mlb_usage_redistribution",
)

KEEP_IN_MODEL = (
    "PlayerImpactResult",
    "UsageRedistribution",
    "MatchupAdjustedImpact",
    "MarketAdjustmentEstimate",
    "player_impact",
    "redistribute_usage",
    "_nba_player_impact",
    "_nba_redistribute_usage",
    "_nba_usage_redistribution",
    "_determine_nba_tier",
)


def _top_level_func_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _top_level_class_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.ClassDef)}


def _reexported_names(path: Path, module_suffix: str) -> set[str]:
    """Names bound in `path` via ImportFrom `*module_suffix` or assignment."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            module_suffix
        ):
            for alias in node.names:
                found.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            dump = ast.dump(node.value)
            if module_suffix.split(".")[-1] not in dump:
                continue
            for t in node.targets:
                if isinstance(t, ast.Name):
                    found.add(t.id)
                elif isinstance(t, ast.Tuple):
                    for elt in t.elts:
                        if isinstance(elt, ast.Name):
                            found.add(elt.id)
    return found


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


def _func_imports_from(path: Path, func_name: str, module_suffix: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == func_name
    )
    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            module_suffix
        ):
            for alias in node.names:
                found.add(alias.asname or alias.name)
    return found


def test_impact_nfl_mlb_defines_the_moved_functions():
    impact_fns = _top_level_func_names(IMPACT)
    for name in MOVED:
        assert name in impact_fns, name
    analysis_fns = _top_level_func_names(ANALYSIS)
    for name in MOVED:
        assert name not in analysis_fns, name


def test_model_keeps_dataclasses_nba_and_dispatchers():
    model_fns = _top_level_func_names(MODEL)
    model_cls = _top_level_class_names(MODEL)
    for name in KEEP_IN_MODEL:
        assert name in model_fns or name in model_cls, name
    impact_fns = _top_level_func_names(IMPACT)
    impact_cls = _top_level_class_names(IMPACT)
    assert "player_impact" not in impact_fns
    assert "redistribute_usage" not in impact_fns
    assert "PlayerImpactResult" not in impact_cls
    assert "UsageRedistribution" not in impact_cls
    assert "_nba_player_impact" not in impact_fns
    assert "_determine_nba_tier" not in impact_fns


def test_model_reexports_impact_nfl_mlb_names():
    reexported = _reexported_names(MODEL, "impact_nfl_mlb")
    for name in MOVED:
        assert name in reexported, name


def test_reexports_are_the_same_objects():
    # Import model first so the circular pair resolves via the facade.
    import tools.injury.model as model
    import tools.injury.impact_nfl_mlb as impact

    for name in MOVED:
        assert getattr(model, name) is getattr(impact, name), name
        assert getattr(model, name).__module__ == "tools.injury.impact_nfl_mlb"


def test_dispatchers_import_extracted_helpers():
    pi_imported = _func_imports_from(MODEL, "player_impact", "impact_nfl_mlb")
    assert "_nfl_player_impact" in pi_imported
    assert "_mlb_player_impact" in pi_imported
    ru_imported = _func_imports_from(MODEL, "redistribute_usage", "impact_nfl_mlb")
    assert "_nfl_usage_redistribution" in ru_imported
    assert "_mlb_usage_redistribution" in ru_imported


def test_compat_shim_and_package_still_resolve():
    import tools.injury as pkg
    import tools.injury.impact_nfl_mlb as impact
    import tools.injury_model as shim

    assert pkg.player_impact is not None
    assert shim.player_impact is not None
    for name in ("player_impact", "redistribute_usage"):
        assert getattr(pkg, name) is getattr(shim, name), name
    for name in MOVED:
        assert getattr(impact, name).__module__ == "tools.injury.impact_nfl_mlb"


def test_line_counts():
    model_n = MODEL.read_text(encoding="utf-8").count("\n")
    impact_n = IMPACT.read_text(encoding="utf-8").count("\n")
    assert model_n < 650, model_n
    assert impact_n >= 300, impact_n


def test_impact_and_model_do_not_import_autonomous():
    assert not _imports_autonomous(IMPACT)
    assert not _imports_autonomous(MODEL)


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
