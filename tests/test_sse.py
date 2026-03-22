"""Tests for the SSE streaming endpoint logic.

The _generate() function inside server.py api_stream() is tested here via a
standalone mirror: _sse_generate().  Keep it in sync with server.py whenever
the SSE algorithm changes.

Key invariants tested:
  1. Subscribe before DB read — no messages are missed
  2. DB replay — lines already written before SSE connected are sent first
  3. Dedup — Redis messages with n < n_from_db are skipped
  4. Terminal status at connect time → done event sent immediately
  5. Done sentinel from Redis closes the stream
  6. 10-second periodic DB fallback catches jobs that finished without sentinel
  7. Keepalive on timeout keeps nginx happy
  8. pubsub is always unsubscribed/closed (finally block)
  9. fakeredis integration — real pub/sub ordering works correctly
"""

from __future__ import annotations

import json
import threading
import time
from typing import Iterator
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

# ---------------------------------------------------------------------------
# Mirror of server.py api_stream._generate() — keep in sync.
# ---------------------------------------------------------------------------

_PREFIX = "mysecond:job:"
_SUFFIX = ":log"


def _sse_generate(job_id: str, registry, redis_client) -> Iterator[str]:
    """Standalone SSE generator — mirrors server.py api_stream._generate()."""

    def _generate():
        ps = redis_client.pubsub()
        ps.subscribe(f"{_PREFIX}{job_id}{_SUFFIX}")
        try:
            lines, status = registry.get_log_and_status_from_db(job_id)
            n_from_db = len(lines)

            for line in lines:
                yield f"data: {json.dumps({'line': line})}\n\n"

            if status in ("done", "failed", "cancelled"):
                yield "event: done\ndata: \n\n"
                return

            last_db_check = time.time()
            while True:
                msg = ps.get_message(ignore_subscribe_messages=True, timeout=5.0)
                if msg is None:
                    yield ": keepalive\n\n"
                    if time.time() - last_db_check > 10:
                        _, cur_status = registry.get_log_and_status_from_db(job_id)
                        if cur_status in ("done", "failed", "cancelled"):
                            yield "event: done\ndata: \n\n"
                            return
                        last_db_check = time.time()
                    continue

                data = json.loads(msg["data"])
                if data.get("done"):
                    yield "event: done\ndata: \n\n"
                    break

                n = data.get("n", 0)
                if n >= n_from_db:
                    yield f"data: {json.dumps({'line': data['text']})}\n\n"
        finally:
            try:
                ps.unsubscribe()
                ps.close()
            except Exception:
                pass

    return _generate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _line_msg(n: int, text: str) -> dict:
    return {"data": json.dumps({"n": n, "text": text})}


def _done_msg(status: str = "done") -> dict:
    return {"data": json.dumps({"done": True, "status": status})}


def _mock_registry(lines=None, status="running") -> MagicMock:
    reg = MagicMock()
    reg.get_log_and_status_from_db.return_value = (lines or [], status)
    return reg


def _mock_pubsub(*messages) -> MagicMock:
    """Mock pubsub whose get_message() returns the given sequence then raises
    StopIteration (via side_effect exhaustion) — tests must consume only
    as many events as expected."""
    ps = MagicMock()
    ps.get_message.side_effect = list(messages)
    return ps


def _mock_redis(ps: MagicMock) -> MagicMock:
    r = MagicMock()
    r.pubsub.return_value = ps
    return r


def _collect(gen_fn, max_events: int = 50) -> list[str]:
    """Drive the generator and collect yielded strings (safety limit)."""
    events = []
    for event in gen_fn():
        events.append(event)
        if len(events) >= max_events:
            break
    return events


# ---------------------------------------------------------------------------
# 1. DB replay
# ---------------------------------------------------------------------------


class TestDbReplay:
    def test_replays_existing_lines(self):
        registry = _mock_registry(["alpha", "beta", "gamma"], "running")
        ps = _mock_pubsub(_done_msg())
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-1", registry, redis))

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 3
        assert json.loads(data_events[0].split("data: ")[1])["line"] == "alpha"
        assert json.loads(data_events[1].split("data: ")[1])["line"] == "beta"
        assert json.loads(data_events[2].split("data: ")[1])["line"] == "gamma"

    def test_empty_db_no_replay(self):
        registry = _mock_registry([], "running")
        ps = _mock_pubsub(_done_msg())
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-2", registry, redis))

        data_events = [e for e in events if e.startswith("data:")]
        assert data_events == []

    def test_subscribe_called_before_db_read(self):
        """Critical invariant: subscribe must happen before DB read to avoid
        missing messages published between the two."""
        call_order = []

        registry = MagicMock()
        registry.get_log_and_status_from_db.side_effect = lambda jid: (
            call_order.append("db_read") or ([], "running")
        )

        ps = MagicMock()
        ps.get_message.side_effect = [_done_msg()]

        def track_subscribe(channel):
            call_order.append("subscribe")
        ps.subscribe.side_effect = track_subscribe

        redis = _mock_redis(ps)
        _collect(_sse_generate("job-sub", registry, redis))

        assert call_order[0] == "subscribe", "subscribe must come before DB read"
        assert call_order[1] == "db_read"


# ---------------------------------------------------------------------------
# 2. Terminal status at connect time
# ---------------------------------------------------------------------------


class TestTerminalStatusAtConnect:
    @pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
    def test_closes_immediately_on_terminal_status(self, status):
        registry = _mock_registry(["line1", "line2"], status)
        ps = _mock_pubsub()  # pubsub never called
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-done", registry, redis))

        assert any("event: done" in e for e in events)
        # get_message should never be called — job was already finished
        ps.get_message.assert_not_called()

    def test_replays_lines_before_done_on_terminal_status(self):
        registry = _mock_registry(["last line"], "done")
        ps = _mock_pubsub()
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-done2", registry, redis))

        data_events = [e for e in events if e.startswith("data:")]
        done_events = [e for e in events if "event: done" in e]
        assert len(data_events) == 1
        assert json.loads(data_events[0].split("data: ")[1])["line"] == "last line"
        assert len(done_events) == 1
        # Lines come before done event
        assert events.index(data_events[0]) < events.index(done_events[0])

    def test_done_event_is_exact_format(self):
        registry = _mock_registry([], "done")
        ps = _mock_pubsub()
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-fmt", registry, redis))

        assert "event: done\ndata: \n\n" in events


# ---------------------------------------------------------------------------
# 3. Realtime streaming
# ---------------------------------------------------------------------------


class TestRealtimeStreaming:
    def test_forwards_redis_lines_in_order(self):
        registry = _mock_registry([], "running")
        ps = _mock_pubsub(
            _line_msg(0, "Starting analysis"),
            _line_msg(1, "Processing position 1"),
            _line_msg(2, "Done"),
            _done_msg(),
        )
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-rt", registry, redis))

        data_events = [e for e in events if e.startswith("data:")]
        texts = [json.loads(e.split("data: ")[1])["line"] for e in data_events]
        assert texts == ["Starting analysis", "Processing position 1", "Done"]

    def test_done_sentinel_closes_stream(self):
        registry = _mock_registry([], "running")
        ps = _mock_pubsub(_line_msg(0, "line"), _done_msg())
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-sentinel", registry, redis))

        assert any("event: done" in e for e in events)
        # get_message must not be called after done sentinel
        assert ps.get_message.call_count == 2  # line msg + done msg

    def test_failed_sentinel_also_closes_stream(self):
        registry = _mock_registry([], "running")
        ps = _mock_pubsub(_done_msg("failed"))
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-fail-sentinel", registry, redis))

        assert any("event: done" in e for e in events)


# ---------------------------------------------------------------------------
# 4. Sequence-number deduplication (the critical race-condition guard)
# ---------------------------------------------------------------------------


class TestSequenceNumberDedup:
    def test_skips_messages_already_sent_from_db(self):
        """3 lines in DB (n=0,1,2).  Worker published 5 lines (n=0..4) before
        SSE subscribed.  SSE should replay n=0,1,2 from DB and only forward
        n=3,n=4 from Redis — never duplicate."""
        registry = _mock_registry(["db0", "db1", "db2"], "running")
        ps = _mock_pubsub(
            _line_msg(0, "db0"),   # duplicate — skip
            _line_msg(1, "db1"),   # duplicate — skip
            _line_msg(2, "db2"),   # duplicate — skip
            _line_msg(3, "new3"),  # new — forward
            _line_msg(4, "new4"),  # new — forward
            _done_msg(),
        )
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-dedup", registry, redis))

        data_events = [e for e in events if e.startswith("data:")]
        texts = [json.loads(e.split("data: ")[1])["line"] for e in data_events]
        # Should have exactly the 3 DB lines + 2 new Redis lines = 5 total
        assert texts == ["db0", "db1", "db2", "new3", "new4"]

    def test_no_dedup_when_db_is_empty(self):
        """0 lines in DB → all Redis messages forwarded (n=0 >= 0)."""
        registry = _mock_registry([], "running")
        ps = _mock_pubsub(
            _line_msg(0, "first"),
            _line_msg(1, "second"),
            _done_msg(),
        )
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-nodedup", registry, redis))

        data_events = [e for e in events if e.startswith("data:")]
        texts = [json.loads(e.split("data: ")[1])["line"] for e in data_events]
        assert texts == ["first", "second"]

    def test_all_redis_messages_are_duplicates(self):
        """5 lines in DB, all 5 Redis messages are duplicates — only DB lines sent."""
        registry = _mock_registry(["l0", "l1", "l2", "l3", "l4"], "running")
        ps = _mock_pubsub(
            _line_msg(0, "l0"),
            _line_msg(1, "l1"),
            _line_msg(2, "l2"),
            _line_msg(3, "l3"),
            _line_msg(4, "l4"),
            _done_msg(),
        )
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-alldup", registry, redis))

        data_events = [e for e in events if e.startswith("data:")]
        texts = [json.loads(e.split("data: ")[1])["line"] for e in data_events]
        # Exactly the 5 DB lines, no duplicates
        assert texts == ["l0", "l1", "l2", "l3", "l4"]

    def test_boundary_message_n_equals_n_from_db_is_forwarded(self):
        """Message with n == n_from_db (not strictly greater) must be forwarded."""
        registry = _mock_registry(["db0"], "running")  # n_from_db = 1
        ps = _mock_pubsub(
            _line_msg(1, "boundary"),  # n=1 == n_from_db=1 → forward
            _done_msg(),
        )
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-boundary", registry, redis))

        data_events = [e for e in events if e.startswith("data:")]
        texts = [json.loads(e.split("data: ")[1])["line"] for e in data_events]
        assert "boundary" in texts


# ---------------------------------------------------------------------------
# 5. Keepalive
# ---------------------------------------------------------------------------


class TestKeepalive:
    def test_keepalive_on_timeout(self):
        """None from get_message (timeout) must emit a keepalive comment."""
        registry = _mock_registry([], "running")
        ps = _mock_pubsub(None, _done_msg())  # one timeout then done
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-keepalive", registry, redis))

        assert ": keepalive\n\n" in events

    def test_multiple_keepalives_before_done(self):
        registry = _mock_registry([], "running")
        ps = _mock_pubsub(None, None, None, _done_msg())
        redis = _mock_redis(ps)

        events = _collect(_sse_generate("job-multi-ka", registry, redis))

        keepalives = [e for e in events if ": keepalive" in e]
        assert len(keepalives) == 3


# ---------------------------------------------------------------------------
# 6. 10-second DB fallback
# ---------------------------------------------------------------------------


class TestPeriodicDbFallback:
    def test_closes_when_fallback_detects_done(self):
        """Redis is silent (worker crashed without sentinel).  After >10s the
        periodic DB check detects 'done' and closes the stream."""
        registry = MagicMock()
        registry.get_log_and_status_from_db.side_effect = [
            ([], "running"),  # initial DB read
            ([], "done"),     # fallback check
        ]
        ps = _mock_pubsub(None)  # one timeout, then side_effect exhausted
        redis = _mock_redis(ps)

        # Fake time: first call=0 (last_db_check), second call=11 (triggers >10s)
        with patch("time.time", side_effect=[0, 11]):
            events = _collect(_sse_generate("job-fallback", registry, redis))

        assert any("event: done" in e for e in events)
        assert registry.get_log_and_status_from_db.call_count == 2

    def test_does_not_close_when_still_running_after_10s(self):
        """Fallback check fires but job still running — must continue streaming."""
        registry = MagicMock()
        registry.get_log_and_status_from_db.side_effect = [
            ([], "running"),   # initial
            ([], "running"),   # fallback → still running, update last_db_check
            ([], "done"),      # second fallback → done
        ]
        ps = _mock_pubsub(None, None)  # two timeouts
        redis = _mock_redis(ps)

        with patch("time.time", side_effect=[0, 11, 11, 22, 22]):
            events = _collect(_sse_generate("job-fallback2", registry, redis))

        assert any("event: done" in e for e in events)

    def test_fallback_not_triggered_before_10s(self):
        """DB fallback must NOT fire if <10s have elapsed."""
        registry = MagicMock()
        registry.get_log_and_status_from_db.side_effect = [
            ([], "running"),
        ]
        ps = _mock_pubsub(None, _done_msg())  # timeout, then sentinel
        redis = _mock_redis(ps)

        # Time barely under 10s — fallback should not fire
        with patch("time.time", side_effect=[0, 9]):
            events = _collect(_sse_generate("job-no-fallback", registry, redis))

        # Only 1 DB call (the initial one), fallback never triggered
        assert registry.get_log_and_status_from_db.call_count == 1

    @pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
    def test_fallback_closes_on_any_terminal_status(self, status):
        registry = MagicMock()
        registry.get_log_and_status_from_db.side_effect = [
            ([], "running"),
            ([], status),
        ]
        ps = _mock_pubsub(None)
        redis = _mock_redis(ps)

        with patch("time.time", side_effect=[0, 11]):
            events = _collect(_sse_generate(f"job-fallback-{status}", registry, redis))

        assert any("event: done" in e for e in events)


# ---------------------------------------------------------------------------
# 7. pubsub cleanup
# ---------------------------------------------------------------------------


class TestPubsubCleanup:
    def test_unsubscribe_called_on_normal_exit(self):
        registry = _mock_registry([], "done")
        ps = _mock_pubsub()
        redis = _mock_redis(ps)

        _collect(_sse_generate("job-cleanup", registry, redis))

        ps.unsubscribe.assert_called_once()
        ps.close.assert_called_once()

    def test_unsubscribe_called_even_if_get_message_raises(self):
        """finally block must clean up pubsub even if an unexpected error occurs."""
        registry = _mock_registry([], "running")
        ps = MagicMock()
        ps.get_message.side_effect = RuntimeError("connection lost")
        redis = _mock_redis(ps)

        with pytest.raises(RuntimeError):
            _collect(_sse_generate("job-error-cleanup", registry, redis))

        ps.unsubscribe.assert_called_once()


# ---------------------------------------------------------------------------
# 8. fakeredis integration — real pub/sub ordering
# ---------------------------------------------------------------------------


class TestFakeredisIntegration:
    """Integration tests using a real in-process Redis emulator.

    These verify that the subscribe-before-read approach actually works with
    real pub/sub semantics: messages published after subscribe() are received,
    messages published before subscribe() are not (pub/sub is not a queue).
    The dedup guard (sequence numbers) compensates for this.
    """

    @pytest.fixture()
    def fakeserver(self):
        return fakeredis.FakeServer()

    @pytest.fixture()
    def publisher(self, fakeserver):
        return fakeredis.FakeRedis(server=fakeserver, decode_responses=True)

    @pytest.fixture()
    def subscriber(self, fakeserver):
        return fakeredis.FakeRedis(server=fakeserver, decode_responses=True)

    def _channel(self, job_id):
        return f"{_PREFIX}{job_id}{_SUFFIX}"

    def test_messages_published_after_subscribe_are_received(self, publisher, subscriber):
        """Core pub/sub property: messages published AFTER subscribe are delivered."""
        job_id = "fakeredis-basic"
        channel = self._channel(job_id)

        registry = _mock_registry([], "running")
        gen = _sse_generate(job_id, registry, subscriber)()

        # Advance past the initial DB-replay phase (no lines, not terminal)
        # The generator is now parked at ps.get_message()

        def publish_then_done():
            time.sleep(0.05)  # let generator subscribe first
            publisher.publish(channel, json.dumps({"n": 0, "text": "hello"}))
            time.sleep(0.05)
            publisher.publish(channel, json.dumps({"done": True, "status": "done"}))

        t = threading.Thread(target=publish_then_done, daemon=True)
        t.start()

        events = []
        for event in gen:
            events.append(event)
            if "event: done" in event:
                break
            if len(events) > 20:
                break
        t.join(timeout=2)

        data_events = [e for e in events if e.startswith("data:")]
        texts = [json.loads(e.split("data: ")[1])["line"] for e in data_events]
        assert "hello" in texts
        assert any("event: done" in e for e in events)

    def test_dedup_prevents_duplicate_lines_on_reconnect(self, publisher, subscriber):
        """Simulate a browser reconnecting mid-job.

        Before SSE connects: worker published n=0,1,2 to Redis (already gone
        from pub/sub — not a queue).  DB has all 3 lines.  Worker then
        publishes n=3,4 after SSE subscribes.

        Result: SSE replays 0,1,2 from DB, then forwards only 3,4 from Redis.
        Total unique lines = 5, no duplicates.
        """
        job_id = "fakeredis-dedup"
        channel = self._channel(job_id)

        # DB already has 3 lines (n=0,1,2 were written before this SSE connection)
        registry = _mock_registry(["line0", "line1", "line2"], "running")
        gen = _sse_generate(job_id, registry, subscriber)()

        def publish_after_subscribe():
            time.sleep(0.05)
            publisher.publish(channel, json.dumps({"n": 3, "text": "line3"}))
            time.sleep(0.05)
            publisher.publish(channel, json.dumps({"n": 4, "text": "line4"}))
            time.sleep(0.05)
            publisher.publish(channel, json.dumps({"done": True, "status": "done"}))

        t = threading.Thread(target=publish_after_subscribe, daemon=True)
        t.start()

        events = []
        for event in gen:
            events.append(event)
            if "event: done" in event:
                break
            if len(events) > 20:
                break
        t.join(timeout=3)

        data_events = [e for e in events if e.startswith("data:")]
        texts = [json.loads(e.split("data: ")[1])["line"] for e in data_events]
        assert texts == ["line0", "line1", "line2", "line3", "line4"]
