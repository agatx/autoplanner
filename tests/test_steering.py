"""Tests for autoplanner.steering — queue draining and QueueSteering."""

from queue import Queue

from autoplanner.steering import _drain_queue, QueueSteering


class TestDrainQueue:
    def test_empty_queue_returns_none(self):
        q: Queue[str] = Queue()
        assert _drain_queue(q) is None

    def test_single_message(self):
        q: Queue[str] = Queue()
        q.put("focus on security")
        assert _drain_queue(q) == "focus on security"

    def test_multiple_messages_joined(self):
        q: Queue[str] = Queue()
        q.put("add more detail")
        q.put("focus on edge cases")
        result = _drain_queue(q)
        assert result == "add more detail\nfocus on edge cases"

    def test_queue_is_empty_after_drain(self):
        q: Queue[str] = Queue()
        q.put("msg")
        _drain_queue(q)
        assert _drain_queue(q) is None


class TestQueueSteering:
    def test_put_then_drain(self):
        qs = QueueSteering()
        qs.put("hello")
        assert qs.drain() == "hello"

    def test_drain_empty(self):
        qs = QueueSteering()
        assert qs.drain() is None

    def test_multiple_puts(self):
        qs = QueueSteering()
        qs.put("first")
        qs.put("second")
        assert qs.drain() == "first\nsecond"

    def test_start_stop_are_noops(self):
        qs = QueueSteering()
        qs.start()
        qs.stop()
        # No exception, no state change
        qs.put("after stop")
        assert qs.drain() == "after stop"
