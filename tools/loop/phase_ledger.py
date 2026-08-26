"""Phase-failure ledger for the ResearchLoop sequencer.

Every phase exception/timeout is recorded here so a "healthy-looking" loop
can't silently swallow failures. Capped at 50 entries; oldest dropped when
full.
"""

import time


class PhaseFailureLedger:
    """Capped ledger of ResearchLoop phase failures."""

    MAX_ENTRIES = 50

    def __init__(self) -> None:
        self._entries: list[dict] = []

    @property
    def count(self) -> int:
        """Number of recorded failures currently retained."""
        return len(self._entries)

    def record(
        self,
        cycle: int,
        phase: str,
        kind: str,
        exc: BaseException | None = None,
    ) -> None:
        """Record a phase failure (exception or timeout).

        ``error`` holds a truncated repr of the exception, or the literal
        string "timeout" when no exception was given.
        """
        self._entries.append(
            {
                "cycle": cycle,
                "phase": phase,
                "kind": kind,
                "error": repr(exc)[:300] if exc is not None else "timeout",
                "ts": time.time(),
            }
        )
        while len(self._entries) > self.MAX_ENTRIES:
            self._entries.pop(0)

    def latest(self, n: int) -> list[dict]:
        """Return up to ``n`` most recent entries, oldest first."""
        if n <= 0:
            return []
        return list(self._entries)[-n:]
