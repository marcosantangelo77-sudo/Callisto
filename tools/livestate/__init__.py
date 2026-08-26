"""tools.livestate — internals of the live game-state collector.

The public API stays on ``tools.live_state`` (the facade). This
package holds the implementation split by concern:

- ``config``    — constants + shared mutable state (single source of truth)
- ``espn``      — HTTP access + per-sport backoff ladder
- ``storage``   — SQLite writes, retention, counters
- ``detectors`` — bridge to tools.live_edges (lazy import)
- ``collector`` — poll_sport, LiveStateCollector, lifecycle helpers

Note the package name avoids shadowing the original module: Python
resolves ``tools.live_state`` (module) and ``tools.livestate``
(package) as distinct entries in ``sys.modules``.
"""

from tools.livestate import (
    collector,
    config,
    detectors,
    espn,
    storage,
)

__all__ = ["collector", "config", "detectors", "espn", "storage"]
