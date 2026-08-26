"""ResearchLoop phase sequencing, extracted from ResearchLoop._loop.

Single source of truth for the ordered research-cycle phases. Each entry
is a PhaseSpec describing one sequential step of the cycle:

    name      — key used in the phase-failure ledger
    method    — bound-method attribute name on ResearchLoop
    timeout   — asyncio.wait_for budget in seconds (None = no wrapper)
    every_n   — run only when ``cycles % every_n == 0`` (None = every cycle)

ResearchLoop._loop iterates PHASES (core) and PERIODIC_PHASES (deferred
when the cycle already blew its time budget) instead of hardcoding the
sequence. Order here IS the execution order — edit with care.
"""

from collections import namedtuple

PhaseSpec = namedtuple("PhaseSpec", ["name", "method", "timeout", "every_n"])


def _spec(name: str, method: str | None = None, timeout: int | None = None,
          every_n: int | None = None) -> PhaseSpec:
    return PhaseSpec(name, method or f"_phase_{name}", timeout, every_n)


#: Core phases, executed in order every cycle (subject to every_n gates).
PHASES: tuple[PhaseSpec, ...] = (
    # Queue drain: burn through deferred work if Claude just became available.
    _spec("queue_drain", method="_drain_deferred_queue", timeout=120),
    _spec("self_repair", timeout=120),            # detect, fix, verify, record
    _spec("self_diagnose", timeout=120),          # pipeline health
    _spec("refresh_signals", timeout=120),        # retroactive threshold updates
    _spec("backtest", timeout=600),               # FIRST — highest priority
    _spec("validate"),                            # sanity checks; no timeout wrapper
    _spec("generate_hypotheses", timeout=300),
    _spec("injury_prop_hypotheses", timeout=120, every_n=4),  # coprime w/ regime/integrity
    _spec("collect_data", timeout=120),
    _spec("embed_data", timeout=120),
    _spec("evaluate", timeout=600),
    _spec("interpret_backtests", timeout=300),
    # Paper trading needs live odds fetches; 120s caused 100% timeout rate.
    _spec("paper_trade", timeout=300),
    _spec("live_execute", timeout=120),
    _spec("review_live", timeout=120),            # LIVE-stage demotion review
    _spec("narrative_edges", timeout=120),
    _spec("claude_deep_work", timeout=300),
)

#: Periodic phases — deferred when core phases consumed > cycle time budget.
PERIODIC_PHASES: tuple[PhaseSpec, ...] = (
    _spec("system_improvement", timeout=120),
    _spec("integrity_check", timeout=120),
    _spec("system_watchdog", timeout=60, every_n=13),  # coprime w/ regime/improvement
    _spec("granger_analysis", timeout=300),
    _spec("regime_analysis", timeout=180),
    _spec("knowledge_compile", timeout=180),
    _spec("knowledge_lint", timeout=120),
)


def phase_names(phases: tuple[PhaseSpec, ...] = PHASES) -> tuple[str, ...]:
    """Ledger names for a phase table, in execution order."""
    return tuple(spec.name for spec in phases)
