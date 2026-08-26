"""
Hypothesis lifecycle manager — facade.

Pipeline:  draft → backtesting → paper_trading → live → retired
           ↘ rejected (at any stage if data actively disproves)
           ↘ paused   (LIVE hypothesis underperforming — demotable)

This module was split into the ``tools.hypothesis`` package:

  tools/hypothesis/config.py        thresholds, gates, env-overridable knobs
  tools/hypothesis/stats.py         pure-Python statistical helpers
  tools/hypothesis/significance.py  evaluate_significance / readiness (mixin)
  tools/hypothesis/promote.py       auto_promote / live review (mixin)
  tools/hypothesis/store.py         CRUD storage methods (mixin)
  tools/hypothesis/sharpening.py    terminal-status wiki hook
  tools/hypothesis/manager.py       HypothesisManager assembled from mixins

Everything public remains importable from here:
    from tools.hypothesis import HypothesisManager, STAGE_ORDER, ...
"""
from tools.hypothesis import *  # noqa: F401,F403
from tools.hypothesis import __all__ as _pkg_all

__all__ = list(_pkg_all)
