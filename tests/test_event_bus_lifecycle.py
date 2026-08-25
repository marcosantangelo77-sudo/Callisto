"""Regression tests: event-bus audit-drain ownership and lifespan shutdown ordering.

Network-free and SQLite-free: the drain loop is exercised with a fake writer
coordinator injected via monkeypatched module references, and the API lifespan
shutdown-ordering invariants are checked with fakes.
"""
import asyncio

import pytest

from tools.event_bus import EventBus

pytestmark = pytest.mark.asyncio


@pytest.fixture
def bus():
    return EventBus()


async def _wait_settle(n_iters=3):
    for _ in range(n_iters):
        await asyncio.sleep(0)


# ── start_audit_drain idempotence ──────────────────────────────────────────


async def test_double_start_creates_single_task(bus):
    await bus.start_audit_drain(db_path=":memory:")
    first = bus._audit_task
    assert first is not None and not first.done()
    await bus.start_audit_drain(db_path=":memory:")
    assert bus._audit_task is first, "double start must not replace the live drainer"
    await bus.stop()


async def test_start_after_done_task_starts_fresh(bus):
    await bus.start_audit_drain(db_path=":memory:")
    first = bus._audit_task
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await _wait_settle()
    await bus.start_audit_drain(db_path=":memory:")
    second = bus._audit_task
    assert second is not None and second is not first and not second.done()
    await bus.stop()


# ── stop idempotence and restart cycle ─────────────────────────────────────


async def test_stop_is_idempotent_and_clears_reference(bus):
    await bus.start_audit_drain(db_path=":memory:")
    task = bus._audit_task
    await bus.stop()
    await _wait_settle()
    assert task.cancelled() or task.done()
    assert bus._audit_task is None
    # Second stop must be a safe no-op.
    await bus.stop()
    assert bus._audit_task is None


async def test_start_stop_start_single_task(bus):
    await bus.start_audit_drain(db_path=":memory:")
    first = bus._audit_task
    await bus.stop()
    await bus.start_audit_drain(db_path=":memory:")
    second = bus._audit_task
    assert second is not first
    assert not second.done()
    assert not first.done() or first.cancelled()
    assert bus._running is True
    await bus.stop()
    await _wait_settle()
    assert bus._audit_task is None
    pending = [t for t in (first, second) if not t.done()]
    assert not pending, f"leaked tasks: {pending}"


# ── drain actually consumes queued events via the coordinator ──────────────


class FakeCoordinator:
    def __init__(self):
        self.batches = []
        self.stopped = False

    async def executemany(self, sql, rows):
        self.batches.append(list(rows))

    async def wait_stopped(self):
        while not self.stopped:
            await asyncio.sleep(0)


async def test_drain_persists_batch_via_coordinator(bus, monkeypatch):
    import tools.event_bus as eb

    coord = FakeCoordinator()
    import tools.db_writer as dbw
    monkeypatch.setattr(dbw, "get_writer_if_running", lambda p: coord)
    bus._running = True
    task = asyncio.create_task(bus._drain_audit(":memory:"))
    bus._audit_task = task
    await bus.publish("edge_detected", {"sport": "nba"})
    # Shorten the 10s batch interval by waiting past it.
    try:
        await asyncio.wait_for(task, timeout=15)
    except asyncio.TimeoutError:
        pass
    finally:
        await bus.stop()
    assert any(r[0] == "edge_detected" for b in coord.batches for r in b), (
        "drain must persist queued audit events"
    )


# ── lifespan shutdown ordering/ownership (source-level invariants) ─────────


async def test_lifespan_shutdown_stops_owned_components_before_writers():
    import inspect

    from api import lifespan

    src = inspect.getsource(lifespan)
    yield_idx = src.index("yield")
    shutdown = src[yield_idx:]

    def order_of(pattern):
        i = shutdown.index(pattern)
        assert i >= 0
        return i

    writers_idx = order_of("_stop_writers()")
    # Producers/drains must be stopped before the write coordinator.
    assert order_of("game_scheduler.stop()") < writers_idx
    assert order_of("get_event_bus().stop()") < writers_idx
    assert order_of("heartbeat.stop()") < writers_idx
    # And each stop is awaited.
    for pat in ("await game_scheduler.stop()", "await get_event_bus().stop()",
                "await heartbeat.stop()"):
        assert pat in shutdown, f"missing awaited stop: {pat}"


async def test_heartbeat_stop_semantics():
    """Heartbeat.stop cancels its own task; verify against a real instance."""
    from tools.self_repair import Heartbeat

    hb = Heartbeat()
    await hb.start()
    assert hb._task is not None and not hb._task.done()
    await hb.stop()
    with pytest.raises(asyncio.CancelledError):
        await hb._task


async def test_game_scheduler_stop_cancels_loop():
    """GameScheduler.stop cancels and awaits its owned loop task."""
    from tools.game_scheduler import GameScheduler

    gs = GameScheduler.__new__(GameScheduler)  # skip __init__ side effects
    gs.event_bus = EventBus()
    gs._games = {}
    gs._running = False
    gs._stopped = asyncio.Event()

    async def _loop():
        while True:
            await asyncio.sleep(3600)

    gs._task = asyncio.create_task(_loop())
    await gs.stop()
    assert gs._task.done()
