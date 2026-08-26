"""Extracted ResearchLoop phase implementations.

``tools.loop.phases_impl`` re-exports phase names so
``from tools.loop.phases_impl import phase_*`` stays valid.

This package must never import ``tools.autonomous``.

Submodules (``post_live``, ``pre_live``, ``hypgen``, ``collect_eval``,
``backtest_run``, ``repair``, ``shared``) are importable directly.
This ``__init__`` does **not** eagerly import those modules: doing so
would cycle when ``phases_impl`` loads ``shared`` (package init runs
before the submodule).
"""
