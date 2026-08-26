"""
tools.hypothesis — package split out of the former single-module
``tools/hypothesis.py`` (now a facade).

Layout:
  config        thresholds, gates, env-overridable knobs, STAGE_ORDER
  stats         pure-Python statistical helpers
  significance  statistical evaluation + promotion readiness (mixin)
  promote       auto_promote / live review / data helpers (mixin)
  store         CRUD storage methods (mixin)
  sharpening    terminal-status wiki hook
  manager       HypothesisManager assembled from mixins

Public API (unchanged): ``from tools.hypothesis import HypothesisManager``
plus STAGE_ORDER, PROMOTION_GATES, validate_model_config, and the stat
helpers.
"""
import importlib as _importlib
import sys as _sys

# Reload the config submodule alongside this facade so that
# ``importlib.reload(tools.hypothesis)`` re-reads env-overridable gate
# thresholds (tests rely on this plumbing).
if "tools.hypothesis.config" in _sys.modules:
    _config = _importlib.reload(_sys.modules["tools.hypothesis.config"])
else:
    _config = _importlib.import_module("tools.hypothesis.config")

for _name in dir(_config):
    if _name.startswith("_") or _name in ("logging", "math", "os", "load_dotenv"):
        continue
    globals()[_name] = getattr(_config, _name)

from tools.hypothesis.stats import (  # noqa: E402,F401
    binomial_pvalue,
    calibration_bins,
    max_drawdown,
    sharpe_ratio,
    ttest_one_sample,
    z_score,
)
from tools.hypothesis.manager import HypothesisManager  # noqa: E402,F401

__all__ = [
    "HypothesisManager",
    "STAGE_ORDER",
    "PROMOTION_GATES",
    "DB_PATH",
    "validate_model_config",
    "get_adaptive_p_value_threshold",
    "binomial_pvalue",
    "ttest_one_sample",
    "z_score",
    "sharpe_ratio",
    "max_drawdown",
    "calibration_bins",
]
