"""
Pace and possession modeling engine — the correct way to project game totals.

This module is now a facade. The implementation was split into the
``tools/pace`` package; all public names are re-exported here for backwards
compatibility with existing ``from tools.pace_model import ...`` callers.
"""

from tools.pace import *  # noqa: F401,F403
from tools.pace import __all__  # noqa: F401
