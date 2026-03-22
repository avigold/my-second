"""Tests for web/worker.py — persistent Redis-backed job worker.

All external I/O (psycopg2, redis, subprocess, OS signals) is mocked so
these tests run without a real DB, Redis, or shell processes.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — must happen before importing worker or runner.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "web"
sys.path.insert(0, str(_WEB))
sys.path.insert(0, str(_ROOT / "src"))

# redis is not installed in this env — inject a stub module before worker
# tries to `import redis as redis_lib` at module level.
if "redis" not in sys.modules:
    _redis_stub = MagicMock(name="redis_module")
    sys.modules["redis"] = _redis_stub

# runner is in web/ (already on path), but mock it so tests don't depend on
# the mysecond CLI being on PATH.
_runner_stub = MagicMock(name="runner")
_runner_stub.build_fetch_argv.return_value = ["mysecond", "fetch-player-games"]
_runner_stub.build_search_argv.return_value = ["mysecond", "search"]
_runner_stub.build_habits_argv.return_value = ["mysecond", "analyse-habits"]
_runner_stub.build_repertoire_argv.return_value = ["mysecond", "extract-repertoire"]
_runner_stub.build_strategise_argv.return_value = ["mysecond", "strategise"]
_runner_stub.build_import_argv.return_value = ["mysecond", "import-pgn-player"]
_runner_stub.build_train_bot_argv.return_value = ["mysecond", "train-bot"]
_runner_stub.build_featured_player_argv.return_value = [
    "mysecond", "train-bot", "--export-white-book", "w.json",
]
sys.modules["runner"] = _runner_stub

import worker  # noqa: E402 — must come after sys.modules setup


# ---------------------------------------------------------------------------
# Mock-building helpers
# ---------------------------------------------------------------------------


def _make_cur() -> MagicMock:
    """Cursor mock that works as a context manager (``with conn.cursor() as cur:``).

    The critical setup is ``cur.__enter__.return_value = cur`` so that
    ``with conn.cursor() as local_cur:`` binds ``local_cur`` to this same mock
    object rather than to a freshly-created child MagicMock.
    """
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None
    return cur


def _make_pool(cur: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cur
    pool = MagicMock()
    pool.getconn.return_value = conn
    return pool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_shutdown():
    """Clear _shutdown event before and after every test."""
    worker._shutdown.clear()
    yield
    worker._shutdown.clear()


@pytest.fixture()
def cur():
    return _make_cur()


@pytest.fixture()
def pool(cur):
    p = _make_pool(cur)
    original = worker._pool
    worker._pool = p
    yield p
    worker._pool = original


@pytest.fixture()
def redis_mock():
    r = MagicMock()
    original = worker._redis
    worker._redis = r
    yield r
    worker._redis = original


@pytest.fixture()
def semaphore():
    sem = threading.Semaphore(10)
    original = worker._semaphore
    worker._semaphore = sem
    yield sem
    worker._semaphore = original


@pytest.fixture(autouse=True)
def reset_runner_mocks():
    """Reset call counts on the runner stub between tests."""
    for fn_name in [
        "build_fetch_argv", "build_search_argv", "build_habits_argv",
        "build_repertoire_argv", "build_strategise_argv", "build_import_argv",
        "build_train_bot_argv", "build_featured_player_argv",
    ]:
        getattr(_runner_stub, fn_name).reset_mock()
    yield


# ---------------------------------------------------------------------------
# _recover_stale
# ---------------------------------------------------------------------------


class TestRecoverStale:
    def test_executes_update_for_running_jobs(self, pool, cur):
        cur.fetchall.return_value = []

        worker._recover_stale()

        sql = cur.execute.call_args[0][0]
        assert "UPDATE jobs" in sql
        assert "failed" in sql
        assert "running" in sql

    def test_commits_after_update(self, pool, cur):
        cur.fetchall.return_value = []

        worker._recover_stale()

        pool.getconn().commit.assert_called()

    def test_marks_multiple_stale_jobs(self, pool, cur):
        cur.fetchall.return_value = [("id-1",), ("id-2",), ("id-3",)]

        # Should complete without error — 3 stale jobs recovered
        worker._recover_stale()

    def test_no_stale_jobs_is_noop(self, pool, cur):
        cur.fetchall.return_value = []

        worker._recover_stale()

        # One execute call (the UPDATE), one commit
        assert cur.execute.call_count == 1


# ---------------------------------------------------------------------------
# _seed_queue
# ---------------------------------------------------------------------------


class TestSeedQueue:
    def test_no_queued_jobs_means_no_rpush(self, pool, cur, redis_mock):
        cur.fetchall.return_value = []

        worker._seed_queue()

        redis_mock.rpush.assert_not_called()

    def test_seeds_each_queued_job(self, pool, cur, redis_mock):
        cur.fetchall.return_value = [("job-1",), ("job-2",), ("job-3",)]

        worker._seed_queue()

        assert redis_mock.rpush.call_count == 3
        redis_mock.rpush.assert_any_call(worker.REDIS_QUEUE_KEY, "job-1")
        redis_mock.rpush.assert_any_call(worker.REDIS_QUEUE_KEY, "job-2")
        redis_mock.rpush.assert_any_call(worker.REDIS_QUEUE_KEY, "job-3")

    def test_seeds_in_fifo_order(self, pool, cur, redis_mock):
        """Oldest job (first DB row) must be RPUSH'd first so BLPOP picks it first."""
        cur.fetchall.return_value = [("oldest",), ("middle",), ("newest",)]

        worker._seed_queue()

        pushed = [c[0][1] for c in redis_mock.rpush.call_args_list]
        assert pushed == ["oldest", "middle", "newest"]

    def test_single_queued_job(self, pool, cur, redis_mock):
        cur.fetchall.return_value = [("only-one",)]

        worker._seed_queue()

        redis_mock.rpush.assert_called_once_with(worker.REDIS_QUEUE_KEY, "only-one")


# ---------------------------------------------------------------------------
# _write_log
# ---------------------------------------------------------------------------


class TestWriteLog:
    def test_joins_lines_with_newline(self, pool, cur):
        worker._write_log("job-abc", ["alpha", "beta", "gamma"])

        args = cur.execute.call_args[0][1]
        assert args[0] == "alpha\nbeta\ngamma"
        assert args[1] == "job-abc"

    def test_empty_list_writes_none(self, pool, cur):
        worker._write_log("job-abc", [])

        args = cur.execute.call_args[0][1]
        assert args[0] is None

    def test_single_line_no_newline_appended(self, pool, cur):
        worker._write_log("job-abc", ["only one line"])

        args = cur.execute.call_args[0][1]
        assert args[0] == "only one line"

    def test_db_error_does_not_propagate(self, pool, cur):
        cur.execute.side_effect = Exception("DB connection reset")

        # Must not raise — write_log is best-effort
        worker._write_log("job-abc", ["line"])


# ---------------------------------------------------------------------------
# _get_db_status
# ---------------------------------------------------------------------------


class TestGetDbStatus:
    def test_returns_status_string(self, pool, cur):
        cur.fetchone.return_value = ("running",)

        result = worker._get_db_status("job-123")

        assert result == "running"

    def test_job_not_found_returns_none(self, pool, cur):
        cur.fetchone.return_value = None

        result = worker._get_db_status("nonexistent-id")

        assert result is None

    def test_db_error_returns_none(self, pool, cur):
        cur.execute.side_effect = Exception("connection lost")

        result = worker._get_db_status("job-err")

        assert result is None

    @pytest.mark.parametrize("status", ["queued", "running", "done", "failed", "cancelled"])
    def test_all_statuses_returned(self, pool, cur, status):
        cur.fetchone.return_value = (status,)

        result = worker._get_db_status("job-x")

        assert result == status


# ---------------------------------------------------------------------------
# _build_argv
# ---------------------------------------------------------------------------


class TestBuildArgv:
    def test_fetch_passes_pgn_out_in_params(self):
        _runner_stub.build_fetch_argv.return_value = ["mysecond", "fetch-player-games"]

        result = worker._build_argv("jid", "fetch", {"username": "Magnus"}, "/out.pgn")

        assert result == _runner_stub.build_fetch_argv.return_value
        call_params = _runner_stub.build_fetch_argv.call_args[0][0]
        assert call_params["pgn_out"] == "/out.pgn"

    def test_fetch_preserves_other_params(self):
        worker._build_argv("jid", "fetch", {"username": "foo", "color": "white"}, "/out.pgn")

        call_params = _runner_stub.build_fetch_argv.call_args[0][0]
        assert call_params["username"] == "foo"
        assert call_params["color"] == "white"

    def test_search_delegates(self):
        result = worker._build_argv("jid", "search", {"side": "white"}, "/out.pgn")

        assert result == _runner_stub.build_search_argv.return_value
        _runner_stub.build_search_argv.assert_called_once_with({"side": "white"}, "/out.pgn")

    def test_habits_delegates(self):
        result = worker._build_argv("jid", "habits", {"username": "foo"}, "/out.pgn")

        assert result == _runner_stub.build_habits_argv.return_value

    def test_repertoire_delegates(self):
        result = worker._build_argv("jid", "repertoire", {"username": "foo"}, "/out.pgn")

        assert result == _runner_stub.build_repertoire_argv.return_value

    def test_strategise_delegates(self):
        result = worker._build_argv("jid", "strategise", {"player": "A", "opponent": "B"}, "/out.pgn")

        assert result == _runner_stub.build_strategise_argv.return_value

    def test_import_uses_job_id_in_pgn_path(self):
        worker._build_argv("MYJOBID-XYZ", "import", {"username": "foo"}, None)

        pgn_path_arg = _runner_stub.build_import_argv.call_args[0][1]
        assert "MYJOBID-XYZ" in pgn_path_arg

    def test_train_bot_regular_uses_train_bot_argv(self):
        result = worker._build_argv("jid", "train-bot", {"opponent_username": "foo"}, "/out.json")

        assert result == _runner_stub.build_train_bot_argv.return_value
        _runner_stub.build_featured_player_argv.assert_not_called()

    def test_train_bot_featured_uses_featured_player_argv(self):
        params = {"opponent_username": "Magnus", "featured_slug": "magnus-carlsen"}

        result = worker._build_argv("jid", "train-bot", params, "/out.json")

        assert result == _runner_stub.build_featured_player_argv.return_value
        _runner_stub.build_train_bot_argv.assert_not_called()

    def test_train_bot_featured_passes_slug_in_paths(self):
        params = {"opponent_username": "Magnus", "featured_slug": "magnus-carlsen"}

        worker._build_argv("jid", "train-bot", params, "/out.json")

        call_args = _runner_stub.build_featured_player_argv.call_args
        # White, black, and profile paths contain the slug
        assert "magnus-carlsen" in call_args[0][2]  # white_book_path
        assert "magnus-carlsen" in call_args[0][3]  # black_book_path
        assert "magnus-carlsen" in call_args[0][4]  # profile_path

    def test_train_bot_featured_regen_profile_false(self):
        params = {"opponent_username": "M", "featured_slug": "slug-x", "regen_profile": False}

        worker._build_argv("jid", "train-bot", params, "/out.json")

        kwargs = _runner_stub.build_featured_player_argv.call_args[1]
        assert kwargs.get("include_profile") is False

    def test_train_bot_featured_regen_profile_defaults_to_true(self):
        params = {"opponent_username": "M", "featured_slug": "slug-y"}  # no regen_profile key

        worker._build_argv("jid", "train-bot", params, "/out.json")

        kwargs = _runner_stub.build_featured_player_argv.call_args[1]
        assert kwargs.get("include_profile") is True

    def test_unknown_command_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown command"):
            worker._build_argv("jid", "undefined-cmd", {}, "/out.pgn")


# ---------------------------------------------------------------------------
# _run_job
# ---------------------------------------------------------------------------


class TestRunJob:
    """Test the core job execution path."""

    def _proc(self, lines=(), returncode=0, pid=999):
        """Build a mock subprocess.Popen instance."""
        proc = MagicMock()
        proc.stdout = iter(lines)
        proc.returncode = returncode
        proc.pid = pid
        return proc

    def test_skips_if_not_claimable(self, pool, cur, redis_mock, semaphore):
        """If UPDATE RETURNING returns nothing, the job was already claimed — skip."""
        cur.fetchone.return_value = None  # claim failed

        with patch("subprocess.Popen") as mock_popen:
            worker._run_job("job-123")

        mock_popen.assert_not_called()

    def test_happy_path_marks_done(self, pool, cur, redis_mock, semaphore):
        cur.fetchone.side_effect = [
            ("fetch", {"username": "foo"}, "/out.pgn"),  # claim
            ("running",),  # _get_db_status (not cancelled)
        ]

        with patch("subprocess.Popen", return_value=self._proc(["Done.\n"], returncode=0)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "fetch"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job") as mock_finish:
            worker._run_job("job-done")

        mock_finish.assert_called_with("job-done", "done", 0)

    def test_non_zero_exit_marks_failed(self, pool, cur, redis_mock, semaphore):
        cur.fetchone.side_effect = [
            ("search", {"side": "white"}, "/out.pgn"),
            ("running",),
        ]

        with patch("subprocess.Popen", return_value=self._proc(["Error!\n"], returncode=1)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "search"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job") as mock_finish:
            worker._run_job("job-fail")

        mock_finish.assert_called_with("job-fail", "failed", 1)

    def test_done_sentinel_published_on_success(self, pool, cur, redis_mock, semaphore):
        cur.fetchone.side_effect = [
            ("fetch", {"username": "foo"}, "/out.pgn"),
            ("running",),
        ]

        with patch("subprocess.Popen", return_value=self._proc(["ok\n"])), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "fetch"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"):
            worker._run_job("job-sentinel")

        sentinels = [
            c for c in redis_mock.publish.call_args_list
            if json.loads(c[0][1]).get("done")
        ]
        assert len(sentinels) == 1
        assert json.loads(sentinels[0][0][1])["status"] == "done"

    def test_done_sentinel_published_on_failure(self, pool, cur, redis_mock, semaphore):
        cur.fetchone.side_effect = [
            ("habits", {"username": "foo"}, "/out.pgn"),
            ("running",),
        ]

        with patch("subprocess.Popen", return_value=self._proc(["err\n"], returncode=1)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "habits"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"):
            worker._run_job("job-fail-sentinel")

        sentinels = [
            c for c in redis_mock.publish.call_args_list
            if json.loads(c[0][1]).get("done")
        ]
        assert json.loads(sentinels[0][0][1])["status"] == "failed"

    def test_cancellation_kills_process_group_at_20_lines(self, pool, cur, redis_mock, semaphore):
        """Cancellation check fires at line 20 — must kill the process group."""
        cur.fetchone.return_value = ("habits", {"username": "foo"}, "/out.pgn")
        lines = [f"processing {i}\n" for i in range(20)]

        with patch("subprocess.Popen", return_value=self._proc(lines, returncode=1, pid=42)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "habits"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_get_db_status", return_value="cancelled"), \
             patch.object(worker, "_finish_job") as mock_finish, \
             patch("os.killpg") as mock_killpg, \
             patch("os.getpgid", return_value=42):
            worker._run_job("job-cancel")

        mock_killpg.assert_called_once_with(42, ANY)
        mock_finish.assert_called_with("job-cancel", "cancelled", ANY)

    def test_external_cancel_detected_at_loop_end(self, pool, cur, redis_mock, semaphore):
        """Even if only 1 line, if DB shows 'cancelled' at final check → cancelled status."""
        cur.fetchone.side_effect = [("fetch", {"username": "foo"}, "/out.pgn")]

        with patch("subprocess.Popen", return_value=self._proc(["one line\n"], returncode=0)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "fetch"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_get_db_status", return_value="cancelled"), \
             patch.object(worker, "_finish_job") as mock_finish:
            worker._run_job("job-ext-cancel")

        mock_finish.assert_called_with("job-ext-cancel", "cancelled", 0)

    def test_heavy_job_acquires_and_releases_semaphore(self, pool, cur, redis_mock):
        cur.fetchone.side_effect = [
            ("habits", {"username": "foo"}, "/out.pgn"),
            ("running",),
        ]
        sem = MagicMock()
        worker._semaphore = sem

        with patch("subprocess.Popen", return_value=self._proc([], returncode=0)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "habits"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"), \
             patch.object(worker, "_post_complete"):
            worker._run_job("job-heavy")

        sem.acquire.assert_called_once()
        sem.release.assert_called_once()

    @pytest.mark.parametrize("cmd", ["search", "habits", "strategise", "repertoire"])
    def test_all_heavy_commands_use_semaphore(self, pool, cur, redis_mock, cmd):
        cur.fetchone.side_effect = [
            (cmd, {}, "/out.pgn"),
            ("running",),
        ]
        sem = MagicMock()
        worker._semaphore = sem

        with patch("subprocess.Popen", return_value=self._proc([], returncode=0)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", cmd]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"), \
             patch.object(worker, "_post_complete"):
            worker._run_job(f"job-{cmd}")

        sem.acquire.assert_called_once()

    @pytest.mark.parametrize("cmd", ["fetch", "import", "train-bot"])
    def test_light_commands_skip_semaphore(self, pool, cur, redis_mock, cmd):
        cur.fetchone.side_effect = [
            (cmd, {"opponent_username": "x"} if cmd == "train-bot" else {}, None),
            ("running",),
        ]
        sem = MagicMock()
        worker._semaphore = sem

        with patch("subprocess.Popen", return_value=self._proc([], returncode=0)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", cmd]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"), \
             patch.object(worker, "_post_complete"):
            worker._run_job(f"job-{cmd}")

        sem.acquire.assert_not_called()
        sem.release.assert_not_called()

    def test_log_lines_published_with_sequence_numbers(self, pool, cur, redis_mock, semaphore):
        cur.fetchone.side_effect = [
            ("fetch", {"username": "foo"}, "/out.pgn"),
            ("running",),
        ]

        with patch("subprocess.Popen", return_value=self._proc(["Alpha\n", "Beta\n", "Gamma\n"])), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "fetch"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"), \
             patch.object(worker, "_post_complete"):
            worker._run_job("job-seq")

        line_msgs = [
            json.loads(c[0][1])
            for c in redis_mock.publish.call_args_list
            if not json.loads(c[0][1]).get("done")
        ]
        assert line_msgs[0] == {"n": 0, "text": "Alpha"}
        assert line_msgs[1] == {"n": 1, "text": "Beta"}
        assert line_msgs[2] == {"n": 2, "text": "Gamma"}

    def test_published_to_correct_channel(self, pool, cur, redis_mock, semaphore):
        cur.fetchone.side_effect = [
            ("fetch", {}, "/out.pgn"),
            ("running",),
        ]
        expected_channel = f"{worker.REDIS_LOG_PREFIX}job-chan-test{worker.REDIS_LOG_SUFFIX}"

        with patch("subprocess.Popen", return_value=self._proc(["line\n"])), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "fetch"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"), \
             patch.object(worker, "_post_complete"):
            worker._run_job("job-chan-test")

        for c in redis_mock.publish.call_args_list:
            assert c[0][0] == expected_channel

    def test_log_flushed_after_first_line(self, pool, cur, redis_mock, semaphore):
        """First line must be flushed immediately (early error visibility)."""
        cur.fetchone.side_effect = [
            ("fetch", {}, "/out.pgn"),
            ("running",),
        ]

        with patch("subprocess.Popen", return_value=self._proc(["First line\n"])), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "fetch"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"), \
             patch.object(worker, "_post_complete"), \
             patch.object(worker, "_write_log") as mock_write:
            worker._run_job("job-flush")

        # At least 2 calls: after line 1 (in-loop), and once more after loop
        assert mock_write.call_count >= 2
        first_call_lines = mock_write.call_args_list[0][0][1]
        assert "First line" in first_call_lines

    def test_unknown_command_marks_failed_and_publishes_sentinel(self, pool, cur, redis_mock, semaphore):
        cur.fetchone.return_value = ("bad-command", {}, None)

        with patch.object(worker, "_finish_job") as mock_finish:
            worker._run_job("job-bad-cmd")

        mock_finish.assert_called_with("job-bad-cmd", "failed", 1)
        sentinels = [
            c for c in redis_mock.publish.call_args_list
            if json.loads(c[0][1]).get("done")
        ]
        assert len(sentinels) == 1

    def test_post_complete_called_after_success(self, pool, cur, redis_mock, semaphore):
        cur.fetchone.side_effect = [
            ("search", {"side": "white"}, "/out.pgn"),
            ("running",),
        ]

        with patch("subprocess.Popen", return_value=self._proc([], returncode=0)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "search"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"), \
             patch.object(worker, "_post_complete") as mock_post:
            worker._run_job("job-post")

        mock_post.assert_called_once_with(
            "job-post", "search", ANY, "/out.pgn", "done", 0
        )

    def test_post_complete_called_after_failure(self, pool, cur, redis_mock, semaphore):
        cur.fetchone.side_effect = [
            ("train-bot", {"opponent_username": "foo"}, "/out.json"),
            ("running",),
        ]

        with patch("subprocess.Popen", return_value=self._proc([], returncode=1)), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "train-bot"]), \
             patch.object(worker, "_set_job_pid"), \
             patch.object(worker, "_finish_job"), \
             patch.object(worker, "_post_complete") as mock_post:
            worker._run_job("job-fail-post")

        mock_post.assert_called_once_with(
            "job-fail-post", "train-bot", ANY, "/out.json", "failed", 1
        )

    def test_semaphore_released_even_on_exception(self, pool, cur, redis_mock):
        """Heavy job semaphore must be released even if Popen raises."""
        cur.fetchone.return_value = ("habits", {"username": "foo"}, "/out.pgn")
        sem = MagicMock()
        worker._semaphore = sem

        with patch("subprocess.Popen", side_effect=OSError("fork failed")), \
             patch.object(worker, "_build_argv", return_value=["mysecond", "habits"]), \
             patch.object(worker, "_finish_job"):
            worker._run_job("job-exc")

        sem.acquire.assert_called_once()
        sem.release.assert_called_once()  # released in finally


# ---------------------------------------------------------------------------
# _dispatch_loop
# ---------------------------------------------------------------------------


class TestDispatchLoop:
    def test_exits_immediately_when_shutdown_set(self, redis_mock):
        worker._shutdown.set()

        worker._dispatch_loop()

        redis_mock.blpop.assert_not_called()

    def test_starts_thread_for_dequeued_job(self, redis_mock):
        call_count = [0]

        def blpop_se(key, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return (worker.REDIS_QUEUE_KEY, "job-dispatch-test")
            worker._shutdown.set()
            return None

        redis_mock.blpop.side_effect = blpop_se

        with patch("threading.Thread") as MockThread:
            inst = MagicMock()
            MockThread.return_value = inst
            worker._dispatch_loop()

        MockThread.assert_called_once()
        assert MockThread.call_args[1]["args"] == ("job-dispatch-test",)
        inst.start.assert_called_once()

    def test_thread_is_daemon(self, redis_mock):
        call_count = [0]

        def blpop_se(key, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return (worker.REDIS_QUEUE_KEY, "job-x")
            worker._shutdown.set()
            return None

        redis_mock.blpop.side_effect = blpop_se

        with patch("threading.Thread") as MockThread:
            MockThread.return_value = MagicMock()
            worker._dispatch_loop()

        assert MockThread.call_args[1].get("daemon") is True

    def test_redis_connection_error_retries(self, redis_mock):
        """A Redis error must not crash the loop — should sleep and retry."""
        call_count = [0]

        def blpop_se(key, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("Redis down")
            worker._shutdown.set()
            return None

        redis_mock.blpop.side_effect = blpop_se

        with patch("time.sleep"):
            worker._dispatch_loop()

        assert call_count[0] == 2  # retried after error

    def test_blpop_timeout_continues_loop(self, redis_mock):
        """BLPOP returning None (timeout) must cause the loop to continue, not exit."""
        call_count = [0]

        def blpop_se(key, timeout):
            call_count[0] += 1
            if call_count[0] >= 3:
                worker._shutdown.set()
            return None  # timeout

        redis_mock.blpop.side_effect = blpop_se

        worker._dispatch_loop()

        assert call_count[0] >= 3

    def test_multiple_jobs_dispatched_sequentially(self, redis_mock):
        """Each BLPOP result gets its own thread."""
        jobs_returned = ["job-a", "job-b", "job-c"]
        results = [(worker.REDIS_QUEUE_KEY, jid) for jid in jobs_returned]
        results.append(None)  # ends loop via shutdown

        call_count = [0]

        def blpop_se(key, timeout):
            r = results[call_count[0]]
            call_count[0] += 1
            if r is None:
                worker._shutdown.set()
            return r

        redis_mock.blpop.side_effect = blpop_se

        thread_args = []

        with patch("threading.Thread") as MockThread:
            def capture(**kwargs):
                thread_args.append(kwargs.get("args", ()))
                t = MagicMock()
                return t
            MockThread.side_effect = capture
            worker._dispatch_loop()

        dispatched_job_ids = [a[0] for a in thread_args]
        assert dispatched_job_ids == jobs_returned


# ---------------------------------------------------------------------------
# _post_complete
# ---------------------------------------------------------------------------


class TestPostComplete:
    def test_non_train_bot_commands_are_noop(self):
        with patch.object(worker, "_handle_bot_complete") as b, \
             patch.object(worker, "_handle_featured_player_complete") as fp:
            for cmd in ("fetch", "search", "habits", "repertoire", "strategise", "import"):
                worker._post_complete("jid", cmd, {}, "/out", "done", 0)

        b.assert_not_called()
        fp.assert_not_called()

    def test_train_bot_regular_calls_handle_bot(self):
        with patch.object(worker, "_handle_bot_complete") as b, \
             patch.object(worker, "_handle_featured_player_complete") as fp:
            worker._post_complete(
                "jid", "train-bot", {"opponent_username": "foo"}, "/out.json", "done", 0
            )

        b.assert_called_once_with("jid", "/out.json", "done")
        fp.assert_not_called()

    def test_train_bot_featured_calls_handle_featured(self):
        params = {"opponent_username": "Magnus", "featured_slug": "magnus-carlsen"}

        with patch.object(worker, "_handle_bot_complete") as b, \
             patch.object(worker, "_handle_featured_player_complete") as fp:
            worker._post_complete("jid", "train-bot", params, "/out.json", "done", 0)

        fp.assert_called_once_with("jid", "magnus-carlsen", params, "/out.json", "done")
        b.assert_not_called()

    def test_exception_in_hook_does_not_propagate(self):
        with patch.object(worker, "_handle_bot_complete", side_effect=RuntimeError("boom")):
            # Must not raise — post_complete is best-effort
            worker._post_complete("jid", "train-bot", {}, "/out.json", "done", 0)

    def test_train_bot_failed_still_calls_handle(self):
        with patch.object(worker, "_handle_bot_complete") as b:
            worker._post_complete(
                "jid", "train-bot", {"opponent_username": "foo"}, "/out.json", "failed", 1
            )

        b.assert_called_once_with("jid", "/out.json", "failed")


# ---------------------------------------------------------------------------
# _handle_bot_complete
# ---------------------------------------------------------------------------


class TestHandleBotComplete:
    def test_no_bot_row_is_noop(self, pool, cur):
        cur.fetchone.return_value = None

        # Must not raise
        worker._handle_bot_complete("job-unknown", "/out.json", "done")

    def test_marks_bot_ready_with_elo_on_success(self, pool, cur, tmp_path):
        out = tmp_path / "result.json"
        out.write_text(json.dumps({"opponent_elo": 2450}))
        cur.fetchone.return_value = ("bot-uuid-abc",)

        worker._handle_bot_complete("job-123", str(out), "done")

        update_calls = [
            c for c in cur.execute.call_args_list
            if "UPDATE bots SET status" in c[0][0]
        ]
        assert len(update_calls) == 1
        # Args: (status, elo, bot_id) or (status, bot_id)
        assert "ready" in update_calls[0][0][1]
        assert 2450 in update_calls[0][0][1]

    def test_marks_bot_ready_without_elo_when_file_missing(self, pool, cur):
        cur.fetchone.return_value = ("bot-uuid-abc",)

        worker._handle_bot_complete("job-123", "/nonexistent/path.json", "done")

        update_calls = [
            c for c in cur.execute.call_args_list
            if "UPDATE bots SET status" in c[0][0]
        ]
        assert len(update_calls) == 1
        assert "ready" in update_calls[0][0][1][0]

    def test_marks_bot_failed_on_failure(self, pool, cur):
        cur.fetchone.return_value = ("bot-uuid-abc",)

        worker._handle_bot_complete("job-123", None, "failed")

        update_calls = [
            c for c in cur.execute.call_args_list
            if "UPDATE bots SET status" in c[0][0]
        ]
        assert "failed" in update_calls[0][0][1][0]

    def test_marks_bot_failed_on_cancel(self, pool, cur):
        cur.fetchone.return_value = ("bot-uuid-abc",)

        worker._handle_bot_complete("job-123", None, "cancelled")

        update_calls = [
            c for c in cur.execute.call_args_list
            if "UPDATE bots SET status" in c[0][0]
        ]
        assert "failed" in update_calls[0][0][1][0]

    def test_db_error_does_not_propagate(self, pool, cur):
        cur.execute.side_effect = Exception("DB error")

        # Must not raise — this is a post-completion hook
        worker._handle_bot_complete("job-123", None, "done")

    def test_commits_after_update(self, pool, cur):
        cur.fetchone.return_value = ("bot-uuid-abc",)

        worker._handle_bot_complete("job-123", None, "done")

        pool.getconn().commit.assert_called()
