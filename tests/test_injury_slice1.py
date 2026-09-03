"""Pin: matchup/market/full injury analysis extracted to tools.injury.analysis.

Does NOT import tools.autonomous (that module hangs this environment).
Does NOT arm live betting. Does NOT add live to paper-signal.
model.py keeps dataclasses, sport-tier helpers, player_impact, and
redistribute_usage; analysis helpers are re-exported (ImportFrom or assignment).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "tools" / "injury" / "model.py"
ANALYSIS = ROOT / "tools" / "injury" / "analysis.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"

MOVED = (
    "matchup_adjusted_impact",
    "estimate_market_adjustment",
    "full_injury_analysis",
    "lookup_position_impact",
)

KEEP_IN_MODEL = (
    "PlayerImpactResult",
    "UsageRedistribution",
    "MatchupAdjustedImpact",
    "MarketAdjustmentEstimate",
    "player_impact",
    "redistribute_usage",
    "_nba_player_impact",
    "_nfl_player_impact",
    "_mlb_player_impact",
    "_determine_nba_tier",
    "_determine_nfl_backup_quality",
    "_determine_mlb_tier",
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


def test_analysis_defines_the_four_functions():
    analysis_fns = _top_level_func_names(ANALYSIS)
    model_fns = _top_level_func_names(MODEL)
    for name in MOVED:
        assert name in analysis_fns, name
        assert name not in model_fns, name


def test_model_keeps_dataclasses_and_player_impact():
    model_fns = _top_level_func_names(MODEL)
    model_cls = _top_level_class_names(MODEL)
    for name in KEEP_IN_MODEL:
        assert name in model_fns or name in model_cls, name
    analysis_fns = _top_level_func_names(ANALYSIS)
    analysis_cls = _top_level_class_names(ANALYSIS)
    assert "player_impact" not in analysis_fns
    assert "redistribute_usage" not in analysis_fns
    assert "PlayerImpactResult" not in analysis_cls
    assert "UsageRedistribution" not in analysis_cls


def test_model_reexports_analysis_names():
    reexported = _reexported_names(MODEL, "analysis")
    for name in MOVED:
        assert name in reexported, name


def test_reexports_are_the_same_objects():
    # Import model first so the circular pair resolves via the facade.
    import tools.injury.model as model
    import tools.injury.analysis as analysis

    for name in MOVED:
        assert getattr(model, name) is getattr(analysis, name), name
        assert getattr(model, name).__module__ == "tools.injury.analysis"


def test_compat_shim_and_package_still_resolve():
    import tools.injury as pkg
    import tools.injury.analysis as analysis
    import tools.injury_model as shim

    for name in MOVED:
        assert getattr(pkg, name) is getattr(analysis, name), name
        assert getattr(shim, name) is getattr(analysis, name), name


def test_line_counts():
    model_n = MODEL.read_text(encoding="utf-8").count("\n")
    analysis_n = ANALYSIS.read_text(encoding="utf-8").count("\n")
    assert model_n < 1000, model_n
    assert analysis_n >= 350, analysis_n


def test_analysis_and_model_do_not_import_autonomous():
    assert not _imports_autonomous(ANALYSIS)
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
