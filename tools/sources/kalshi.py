"""Kalshi source adapter — thin re-export over the domain package.

The registry instantiates tools.sources.<name> by convention; the real
implementation (SPEC, KalshiAdapter) lives in tools/domains/kalshi/
because that package also carries the resolver wiring and plugin. This
module adds nothing.
"""

from tools.domains.kalshi.market import SPEC, KalshiAdapter  # noqa: F401
