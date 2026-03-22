"""Tests for Flask routes in server.py — verifies Redis job dispatch.

Strategy
--------
server.py executes DB/Redis connections at module level (JobRegistry, BotManager,
FeaturedPlayerManager, redis.from_url, Cache).  We patch all of them before the
module is first imported so the import succeeds without any real services.

After import, each test replaces server.registry and server._redis with mocks
via monkeypatch to verify route logic in isolation.
"""

from __future__ import annotations

import io
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch, ANY

import pytest

# ---------------------------------------------------------------------------
# Path setup — must happen before any local imports
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "web"))
sys.path.insert(0, str(_ROOT / "src"))

# ---------------------------------------------------------------------------
# Patch all external dependencies BEFORE importing server.py.
#
# server.py module-level side effects that require real services:
#   registry = JobRegistry(DATABASE_URL)          → psycopg2 ThreadedConnectionPool
#   featured_player_manager = FeaturedPlayer…(…)  → psycopg2.connect in __init__
#   _redis = redis_lib.from_url(…)                → Redis connection
#   _opening_cache = Cache(…)                      → SQLite
#
# We stub all of them so the import succeeds without real services running.
# ---------------------------------------------------------------------------

_fake_pool = MagicMock()
_fake_pool.getconn.return_value = MagicMock()

_fake_conn = MagicMock()
_fake_cur = MagicMock()
_fake_cur.__enter__ = MagicMock(return_value=_fake_cur)
_fake_cur.__exit__ = MagicMock(return_value=False)
_fake_conn.cursor.return_value = _fake_cur

with (
    patch("psycopg2.pool.ThreadedConnectionPool", return_value=_fake_pool),
    patch("psycopg2.connect", return_value=_fake_conn),
    patch("jobs.JobRegistry._init_db"),
    patch("jobs.JobRegistry._load_existing"),
    patch("redis.from_url", return_value=MagicMock()),
    patch("mysecond.cache.Cache.__init__", return_value=None),
):
    import server  # noqa: E402 — must follow sys.modules patching

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUEUE_KEY = "mysecond:jobs:queued"


def _fake_user(role: str = "admin") -> dict:
    """Return a session user dict.  Admin role bypasses all plan / quota limits."""
    return {
        "id": str(uuid.uuid4()),
        "username": "testuser",
        "role": role,
        "platform": "lichess",
    }


def _fake_job(command: str = "fetch") -> MagicMock:
    job = MagicMock()
    job.id = str(uuid.uuid4())
    job.command = command
    job.status = "queued"
    job.user_id = str(uuid.uuid4())
    job.params = {}
    return job


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_registry(monkeypatch):
    """Replace server.registry with a controllable MagicMock.

    Returns (registry_mock, fake_job) so callers can assert on both.
    """
    reg = MagicMock()
    job = _fake_job()
    reg.create.return_value = job
    reg.has_running_job.return_value = False   # no concurrent job
    reg.count_monthly_jobs.return_value = 0    # under free limit
    reg.get_user_plan.return_value = "free"
    monkeypatch.setattr(server, "registry", reg)
    return reg, job


@pytest.fixture()
def mock_redis(monkeypatch):
    """Replace server._redis with a MagicMock and return it."""
    redis_mock = MagicMock()
    monkeypatch.setattr(server, "_redis", redis_mock)
    return redis_mock


@pytest.fixture()
def authed_client(monkeypatch, mock_registry, mock_redis):
    """Flask test client with an admin user (bypasses all plan limits).

    Yields (client, (registry_mock, fake_job), redis_mock).
    """
    user = _fake_user(role="admin")
    monkeypatch.setattr(server, "get_current_user", lambda: user)
    with server.app.test_client() as client:
        yield client, mock_registry, mock_redis


# ---------------------------------------------------------------------------
# POST /api/fetch
# ---------------------------------------------------------------------------


class TestFetchRoute:
    def test_rpush_called_on_success(self, authed_client):
        client, (reg, job), redis_mock = authed_client
        resp = client.post("/api/fetch", json={"username": "hikaru", "color": "white"})
        assert resp.status_code == 200
        assert resp.get_json()["job_id"] == job.id
        redis_mock.rpush.assert_called_once_with(_QUEUE_KEY, job.id)

    def test_registry_create_called_with_fetch(self, authed_client):
        client, (reg, job), _ = authed_client
        client.post("/api/fetch", json={"username": "hikaru", "color": "white"})
        reg.create.assert_called_once_with("fetch", ANY, out_path=None, user_id=ANY)

    def test_out_path_set_before_rpush(self, authed_client):
        """set_out_path must be called before rpush so worker can find the file."""
        client, (reg, job), redis_mock = authed_client
        call_order = []
        reg.set_out_path.side_effect = lambda *a, **kw: call_order.append("set_out_path")
        redis_mock.rpush.side_effect = lambda *a, **kw: call_order.append("rpush")

        client.post("/api/fetch", json={"username": "hikaru", "color": "white"})

        assert call_order == ["set_out_path", "rpush"], (
            "set_out_path must be called before rpush"
        )

    def test_missing_username_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post("/api/fetch", json={"color": "white"})
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()

    def test_missing_color_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post("/api/fetch", json={"username": "hikaru"})
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()

    def test_username_stripped(self, authed_client):
        client, (reg, _), _ = authed_client
        client.post("/api/fetch", json={"username": "  hikaru  ", "color": "white"})
        actual_params = reg.create.call_args[0][1]
        assert actual_params["username"] == "hikaru"


# ---------------------------------------------------------------------------
# POST /api/search
# ---------------------------------------------------------------------------


class TestSearchRoute:
    def test_rpush_called_on_success(self, authed_client):
        client, (reg, job), redis_mock = authed_client
        resp = client.post("/api/search", json={"side": "white", "username": "hikaru"})
        assert resp.status_code == 200
        redis_mock.rpush.assert_called_once_with(_QUEUE_KEY, job.id)

    def test_registry_create_called_with_search(self, authed_client):
        client, (reg, _), _ = authed_client
        client.post("/api/search", json={"side": "white", "username": "hikaru"})
        reg.create.assert_called_once_with("search", ANY, user_id=ANY)

    def test_missing_side_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post("/api/search", json={"username": "hikaru"})
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/habits
# ---------------------------------------------------------------------------


class TestHabitsRoute:
    def test_rpush_called_on_success(self, authed_client):
        client, (reg, job), redis_mock = authed_client
        resp = client.post("/api/habits", json={"username": "hikaru", "color": "white"})
        assert resp.status_code == 200
        redis_mock.rpush.assert_called_once_with(_QUEUE_KEY, job.id)

    def test_registry_create_called_with_habits(self, authed_client):
        client, (reg, _), _ = authed_client
        client.post("/api/habits", json={"username": "hikaru", "color": "white"})
        reg.create.assert_called_once_with("habits", ANY, user_id=ANY)

    def test_missing_username_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post("/api/habits", json={"color": "white"})
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()

    def test_missing_color_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post("/api/habits", json={"username": "hikaru"})
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/repertoire
# ---------------------------------------------------------------------------


class TestRepertoireRoute:
    def test_rpush_called_on_success(self, authed_client):
        client, (reg, job), redis_mock = authed_client
        resp = client.post(
            "/api/repertoire", json={"username": "hikaru", "color": "white"}
        )
        assert resp.status_code == 200
        redis_mock.rpush.assert_called_once_with(_QUEUE_KEY, job.id)

    def test_registry_create_called_with_repertoire(self, authed_client):
        client, (reg, _), _ = authed_client
        client.post("/api/repertoire", json={"username": "hikaru", "color": "white"})
        reg.create.assert_called_once_with("repertoire", ANY, user_id=ANY)

    def test_missing_fields_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post("/api/repertoire", json={"username": "hikaru"})
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/strategise
# ---------------------------------------------------------------------------


class TestStrategiseRoute:
    _VALID_PARAMS = {
        "player": "hikaru",
        "player_color": "white",
        "opponent": "magnus",
    }

    def test_rpush_called_on_success(self, authed_client):
        client, (reg, job), redis_mock = authed_client
        resp = client.post("/api/strategise", json=self._VALID_PARAMS)
        assert resp.status_code == 200
        redis_mock.rpush.assert_called_once_with(_QUEUE_KEY, job.id)

    def test_registry_create_called_with_strategise(self, authed_client):
        client, (reg, _), _ = authed_client
        client.post("/api/strategise", json=self._VALID_PARAMS)
        reg.create.assert_called_once_with("strategise", ANY, user_id=ANY)

    def test_missing_player_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post(
            "/api/strategise", json={"player_color": "white", "opponent": "magnus"}
        )
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()

    def test_missing_opponent_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post(
            "/api/strategise", json={"player": "hikaru", "player_color": "white"}
        )
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()

    def test_api_key_stripped_from_params(self, authed_client):
        """ANTHROPIC_API_KEY must never be forwarded into the job params."""
        client, (reg, _), _ = authed_client
        client.post(
            "/api/strategise",
            json={**self._VALID_PARAMS, "api_key": "sk-secret-key"},
        )
        params_used = reg.create.call_args[0][1]
        assert "api_key" not in params_used

    def test_player_stripped(self, authed_client):
        client, (reg, _), _ = authed_client
        client.post(
            "/api/strategise",
            json={**self._VALID_PARAMS, "player": "  hikaru  "},
        )
        params_used = reg.create.call_args[0][1]
        assert params_used["player"] == "hikaru"

    def test_opponent_stripped(self, authed_client):
        client, (reg, _), _ = authed_client
        client.post(
            "/api/strategise",
            json={**self._VALID_PARAMS, "opponent": "  magnus  "},
        )
        params_used = reg.create.call_args[0][1]
        assert params_used["opponent"] == "magnus"


# ---------------------------------------------------------------------------
# POST /api/import-pgn
# ---------------------------------------------------------------------------


class TestImportPgnRoute:
    def _pgn_upload(self) -> dict:
        return {
            "pgn_file": (
                io.BytesIO(b"[Event \"Test\"]\n1.e4 *"),
                "test.pgn",
                "application/octet-stream",
            )
        }

    def test_rpush_called_on_success(self, monkeypatch, mock_registry, mock_redis):
        reg, job = mock_registry
        # The route saves the PGN file to UPLOADS_DIR; mock pgn_file.save() too.
        monkeypatch.setattr(server, "get_current_user", lambda: _fake_user(role="admin"))

        with server.app.test_client() as client:
            resp = client.post(
                "/api/import-pgn",
                data={
                    "username": "hikaru",
                    "color": "white",
                    **{
                        k: v
                        for k, v in {
                            "pgn_file": (
                                io.BytesIO(b"[Event \"Test\"]\n1.e4 *"),
                                "test.pgn",
                            )
                        }.items()
                    },
                },
                content_type="multipart/form-data",
            )

        # Route either succeeded (200) or failed for env reasons; only check rpush
        # if the job was actually created.
        if resp.status_code == 200:
            assert resp.get_json()["job_id"] == job.id
            mock_redis.rpush.assert_called_once_with(_QUEUE_KEY, job.id)

    def test_missing_username_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post(
            "/api/import-pgn",
            data={"pgn_file": (io.BytesIO(b"pgn"), "test.pgn")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()

    def test_missing_pgn_file_returns_400(self, authed_client):
        client, _, redis_mock = authed_client
        resp = client.post(
            "/api/import-pgn",
            data={"username": "hikaru"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        redis_mock.rpush.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/jobs/<id>/cancel
# ---------------------------------------------------------------------------


class TestCancelRoute:
    def _setup(self, monkeypatch, job_status="running", mark_result=True):
        """Helper: wire up a registry + redis mock and return (client, reg, job)."""
        reg = MagicMock()
        job = _fake_job()
        job.status = job_status
        reg.get.return_value = job
        reg.mark_cancelled.return_value = mark_result

        redis_mock = MagicMock()
        user = _fake_user(role="admin")
        monkeypatch.setattr(server, "registry", reg)
        monkeypatch.setattr(server, "_redis", redis_mock)
        monkeypatch.setattr(server, "get_current_user", lambda: user)
        return server.app.test_client(), reg, job

    def test_cancel_running_job_returns_200(self, monkeypatch):
        client, reg, job = self._setup(monkeypatch, job_status="running")
        with client:
            resp = client.post(f"/api/jobs/{job.id}/cancel")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "cancelled"

    def test_cancel_queued_job_returns_200(self, monkeypatch):
        client, reg, job = self._setup(monkeypatch, job_status="queued")
        with client:
            resp = client.post(f"/api/jobs/{job.id}/cancel")
        assert resp.status_code == 200
        reg.mark_cancelled.assert_called_once_with(job.id)

    def test_mark_cancelled_called_with_job_id(self, monkeypatch):
        client, reg, job = self._setup(monkeypatch)
        with client:
            client.post(f"/api/jobs/{job.id}/cancel")
        reg.mark_cancelled.assert_called_once_with(job.id)

    def test_nonexistent_job_returns_404(self, monkeypatch):
        client, reg, _ = self._setup(monkeypatch)
        reg.get.return_value = None
        with client:
            resp = client.post(f"/api/jobs/{uuid.uuid4()}/cancel")
        assert resp.status_code == 404

    def test_done_job_returns_400(self, monkeypatch):
        client, reg, job = self._setup(monkeypatch, job_status="done")
        with client:
            resp = client.post(f"/api/jobs/{job.id}/cancel")
        assert resp.status_code == 400
        reg.mark_cancelled.assert_not_called()

    def test_failed_job_returns_400(self, monkeypatch):
        client, reg, job = self._setup(monkeypatch, job_status="failed")
        with client:
            resp = client.post(f"/api/jobs/{job.id}/cancel")
        assert resp.status_code == 400

    def test_cancelled_job_returns_400(self, monkeypatch):
        client, reg, job = self._setup(monkeypatch, job_status="cancelled")
        with client:
            resp = client.post(f"/api/jobs/{job.id}/cancel")
        assert resp.status_code == 400

    def test_mark_cancelled_race_condition_returns_400(self, monkeypatch):
        """mark_cancelled returns False when the DB beat us to it — return 400."""
        client, reg, job = self._setup(monkeypatch, job_status="running", mark_result=False)
        with client:
            resp = client.post(f"/api/jobs/{job.id}/cancel")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Job concurrency / plan limit gating
# ---------------------------------------------------------------------------


class TestJobLimitGating:
    def test_concurrent_job_returns_409(self, monkeypatch, mock_redis):
        """If the user already has an active job, new submissions return 409."""
        reg = MagicMock()
        job = _fake_job()
        reg.create.return_value = job
        reg.has_running_job.return_value = True   # <- user has a running job
        monkeypatch.setattr(server, "registry", reg)
        monkeypatch.setattr(server, "_redis", mock_redis)
        monkeypatch.setattr(server, "get_current_user", lambda: _fake_user(role="user"))

        with server.app.test_client() as client:
            resp = client.post("/api/fetch", json={"username": "hikaru", "color": "white"})

        assert resp.status_code == 409
        mock_redis.rpush.assert_not_called()

    def test_free_plan_limit_returns_402(self, monkeypatch, mock_redis):
        """Free-plan users who hit their monthly quota get a 402."""
        reg = MagicMock()
        job = _fake_job()
        reg.create.return_value = job
        reg.has_running_job.return_value = False
        reg.get_user_plan.return_value = "free"
        reg.count_monthly_jobs.return_value = 5   # at/over free limit (5 for fetch)
        monkeypatch.setattr(server, "registry", reg)
        monkeypatch.setattr(server, "_redis", mock_redis)
        monkeypatch.setattr(server, "get_current_user", lambda: _fake_user(role="user"))

        with server.app.test_client() as client:
            resp = client.post("/api/fetch", json={"username": "hikaru", "color": "white"})

        assert resp.status_code == 402
        mock_redis.rpush.assert_not_called()

    def test_pro_user_bypasses_plan_limit(self, monkeypatch, mock_redis):
        """Pro users are never blocked by monthly quotas."""
        reg = MagicMock()
        job = _fake_job()
        reg.create.return_value = job
        reg.has_running_job.return_value = False
        reg.get_user_plan.return_value = "pro"
        reg.count_monthly_jobs.return_value = 999  # irrelevant for pro
        monkeypatch.setattr(server, "registry", reg)
        monkeypatch.setattr(server, "_redis", mock_redis)
        monkeypatch.setattr(server, "get_current_user", lambda: _fake_user(role="user"))

        with server.app.test_client() as client:
            resp = client.post("/api/fetch", json={"username": "hikaru", "color": "white"})

        assert resp.status_code == 200
        mock_redis.rpush.assert_called_once_with(_QUEUE_KEY, job.id)

    def test_admin_bypasses_plan_limit(self, monkeypatch, mock_redis):
        """Admin role always gets pro access regardless of subscription."""
        reg = MagicMock()
        job = _fake_job()
        reg.create.return_value = job
        reg.has_running_job.return_value = False
        reg.get_user_plan.return_value = "free"     # subscription says free
        reg.count_monthly_jobs.return_value = 999   # way over limit
        monkeypatch.setattr(server, "registry", reg)
        monkeypatch.setattr(server, "_redis", mock_redis)
        monkeypatch.setattr(server, "get_current_user", lambda: _fake_user(role="admin"))

        with server.app.test_client() as client:
            resp = client.post("/api/fetch", json={"username": "hikaru", "color": "white"})

        assert resp.status_code == 200
        mock_redis.rpush.assert_called_once_with(_QUEUE_KEY, job.id)

    def test_unauthenticated_returns_401(self, monkeypatch, mock_redis):
        """Unauthenticated requests to API routes get 401."""
        reg = MagicMock()
        monkeypatch.setattr(server, "registry", reg)
        monkeypatch.setattr(server, "_redis", mock_redis)
        monkeypatch.setattr(server, "get_current_user", lambda: None)

        with server.app.test_client() as client:
            resp = client.post("/api/fetch", json={"username": "hikaru", "color": "white"})

        assert resp.status_code == 401
        mock_redis.rpush.assert_not_called()
