"""Tests for deferred work queue, local fallbacks, and downtime tracker."""

import asyncio
import json
import os
import tempfile

import pytest

# Use a temp DB for tests
_test_db = os.path.join(tempfile.gettempdir(), "test_work_queue.db")


@pytest.fixture(autouse=True)
def clean_db():
    """Remove test DB before each test."""
    if os.path.exists(_test_db):
        os.remove(_test_db)
    yield
    if os.path.exists(_test_db):
        os.remove(_test_db)


# ── DeferredWorkQueue tests ──

class TestDeferredWorkQueue:
    def test_enqueue_and_size(self):
        async def _test():
            from tools.work_queue import DeferredWorkQueue
            q = DeferredWorkQueue(db_path=_test_db)
            assert await q.size() == 0
            await q.enqueue("hypothesis_gen", "test prompt 1", priority=2)
            assert await q.size() == 1
            await q.enqueue("deep_work", "test prompt 2", priority=5)
            assert await q.size() == 2
        asyncio.run(_test())

    def test_drain_returns_highest_priority_first(self):
        async def _test():
            from tools.work_queue import DeferredWorkQueue
            q = DeferredWorkQueue(db_path=_test_db)
            await q.enqueue("low_priority", "prompt low", priority=9)
            await q.enqueue("high_priority", "prompt high", priority=1)
            await q.enqueue("medium_priority", "prompt mid", priority=5)

            items = await q.drain(max_items=3)
            assert len(items) == 3
            assert items[0]["work_type"] == "high_priority"
            assert items[1]["work_type"] == "medium_priority"
            assert items[2]["work_type"] == "low_priority"
        asyncio.run(_test())

    def test_drain_respects_max_items(self):
        async def _test():
            from tools.work_queue import DeferredWorkQueue
            q = DeferredWorkQueue(db_path=_test_db)
            for i in range(10):
                await q.enqueue(f"type_{i}", f"prompt {i}", priority=i)
            items = await q.drain(max_items=3)
            assert len(items) == 3
        asyncio.run(_test())

    def test_mark_done_removes_from_pending(self):
        async def _test():
            from tools.work_queue import DeferredWorkQueue
            q = DeferredWorkQueue(db_path=_test_db)
            await q.enqueue("test", "prompt", priority=1)
            assert await q.size() == 1

            items = await q.drain(max_items=1)
            assert len(items) == 1
            # During drain, item is in "draining" state
            assert await q.size() == 0  # no longer "pending"

            await q.mark_done(items[0]["id"], "success")
            status = await q.get_status()
            assert status["done"] == 1
            assert status["pending"] == 0
        asyncio.run(_test())

    def test_mark_failed_returns_to_pending(self):
        async def _test():
            from tools.work_queue import DeferredWorkQueue
            q = DeferredWorkQueue(db_path=_test_db)
            await q.enqueue("test", "prompt", priority=1)
            items = await q.drain(max_items=1)
            await q.mark_failed(items[0]["id"], "rate_limited")
            assert await q.size() == 1  # back to pending
        asyncio.run(_test())

    def test_queue_cap_at_50(self):
        async def _test():
            from tools.work_queue import DeferredWorkQueue
            q = DeferredWorkQueue(db_path=_test_db)
            for i in range(55):
                await q.enqueue(f"type_{i}", f"prompt {i}", priority=i % 10)
            assert await q.size() <= 50
        asyncio.run(_test())

    def test_get_status(self):
        async def _test():
            from tools.work_queue import DeferredWorkQueue
            q = DeferredWorkQueue(db_path=_test_db)
            await q.enqueue("test1", "p1", priority=1)
            await q.enqueue("test2", "p2", priority=2)
            status = await q.get_status()
            assert status["pending"] == 2
            assert status["done"] == 0
        asyncio.run(_test())

    def test_persistence_across_instances(self):
        """Queue persists to SQLite, survives new instances."""
        async def _test():
            from tools.work_queue import DeferredWorkQueue
            q1 = DeferredWorkQueue(db_path=_test_db)
            await q1.enqueue("persist_test", "important prompt", priority=1)
            assert await q1.size() == 1

            # New instance, same DB
            q2 = DeferredWorkQueue(db_path=_test_db)
            assert await q2.size() == 1
            items = await q2.drain(max_items=1)
            assert items[0]["work_type"] == "persist_test"
            assert items[0]["prompt"] == "important prompt"
        asyncio.run(_test())


# ── ClaudeDowntimeTracker tests ──

class TestClaudeDowntimeTracker:
    def test_outage_tracking(self):
        from tools.work_queue import ClaudeDowntimeTracker
        tracker = ClaudeDowntimeTracker()
        assert not tracker.is_in_outage
        assert tracker.get_status()["total_outages"] == 0

        tracker.mark_unavailable()
        assert tracker.is_in_outage
        assert tracker.get_status()["total_outages"] == 1

        tracker.item_queued()
        tracker.item_queued()
        assert tracker.get_status()["items_queued_this_outage"] == 2

        tracker.mark_available()
        assert not tracker.is_in_outage
        assert tracker.get_status()["total_outages"] == 1

    def test_double_unavailable_no_double_count(self):
        from tools.work_queue import ClaudeDowntimeTracker
        tracker = ClaudeDowntimeTracker()
        tracker.mark_unavailable()
        tracker.mark_unavailable()  # should not increment
        assert tracker.get_status()["total_outages"] == 1

    def test_multiple_outages(self):
        from tools.work_queue import ClaudeDowntimeTracker
        tracker = ClaudeDowntimeTracker()
        tracker.mark_unavailable()
        tracker.mark_available()
        tracker.mark_unavailable()
        tracker.mark_available()
        assert tracker.get_status()["total_outages"] == 2

    def test_status_fields(self):
        from tools.work_queue import ClaudeDowntimeTracker
        tracker = ClaudeDowntimeTracker()
        status = tracker.get_status()
        assert "in_outage" in status
        assert "current_outage_seconds" in status
        assert "items_queued_this_outage" in status
        assert "total_outages" in status
        assert "total_downtime_seconds" in status
        assert "last_outage_duration_seconds" in status


# ── Local fallback tests ──

class TestLocalFallbacks:
    def test_local_fallback_interpret_rejects_zero_signal(self):
        async def _test():
            from tools.work_queue import local_fallback_interpret
            hypo_data = [
                {"id": "h1", "name": "test_hypo", "signals": 0, "events": 100,
                 "avg_edge": 0, "hit_rate": 0, "wins": 0, "losses": 0},
            ]
            result = await local_fallback_interpret(hypo_data)
            assert "h1" in result["reject"]
        asyncio.run(_test())

    def test_local_fallback_interpret_rejects_negative_edge(self):
        async def _test():
            from tools.work_queue import local_fallback_interpret
            hypo_data = [
                {"id": "h2", "name": "neg_edge", "signals": 5, "events": 50,
                 "avg_edge": -0.05, "hit_rate": 0.4, "wins": 12, "losses": 20},
            ]
            result = await local_fallback_interpret(hypo_data)
            assert "h2" in result["reject"]
        asyncio.run(_test())

    def test_local_fallback_interpret_identifies_promising(self):
        async def _test():
            from tools.work_queue import local_fallback_interpret
            hypo_data = [
                {"id": "h3", "name": "good_hypo", "signals": 20, "events": 80,
                 "avg_edge": 0.04, "hit_rate": 0.56, "wins": 30, "losses": 20},
            ]
            result = await local_fallback_interpret(hypo_data)
            assert "h3" not in result.get("reject", [])
            assert "promising" in result.get("insights", "").lower()
        asyncio.run(_test())

    def test_local_fallback_interpret_handles_empty(self):
        async def _test():
            from tools.work_queue import local_fallback_interpret
            result = await local_fallback_interpret([])
            assert result["reject"] == []
        asyncio.run(_test())

    def test_local_fallback_deep_work_without_db(self):
        async def _test():
            from tools.work_queue import local_fallback_deep_work
            result = await local_fallback_deep_work(None)
            assert result["reject_ids"] == []
            assert result["new_hypotheses"] == []
        asyncio.run(_test())
