# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for the TTL cleanup processor's delete decision (``TTLCleanupService``).

The scheduler half (AGFS walk + enqueue) is exercised e2e; these focus on the
processor's per-object decision, which owns the correctness-critical races:

- renewal-wins / don't-delete-a-new-object: re-read the *live* expiry right
  before deleting and skip if it is no longer expired,
- idempotent re-delivery: a terminal task record never runs the delete twice,
- already-gone: a NotFoundError settles as a successful strict cleanup,
- delete failure: the task is failed and the object retried next scan,
- strict deletes: sessions/events are torn down with ``strict=True`` so billing
  only stops once files and vectors are confirmed gone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import openviking.core.ttl as core_ttl
import openviking.service.ttl_cleanup as ttl_cleanup
from openviking.core.ttl import OBJECT_TYPE_EVENT, OBJECT_TYPE_SESSION
from openviking.service.task_tracker import TaskStatus, TaskTracker, set_task_tracker
from openviking_cli.exceptions import NotFoundError


class _TaskStore:
    def __init__(self):
        self.tasks = {}

    async def create(self, task):
        self.tasks[task.task_id] = task

    async def update(self, task):
        self.tasks[task.task_id] = task

    async def get(self, task_id, *, account_id=None, user_id=None):
        return None

    async def list(self, account_id, *, user_id=None):
        return []

    async def delete(self, task_id, *, account_id, user_id=None):
        self.tasks.pop(task_id, None)


SESSION_URI = "viking://user/u1/sessions/s1"
EVENT_URI = "viking://user/u1/memories/events/2026/e.md"


def _session_meta(expires_at: str) -> str:
    return json.dumps({"expires_at": expires_at})


def _event_body(expires_at: str) -> str:
    fields = {"expires_at": expires_at}
    return f"<!-- MEMORY_FIELDS {json.dumps(fields)} -->\nbody text"


def _message(object_type: str, *, object_uri: str, session_id=None) -> dict:
    return {
        "task_id": "task-1",
        "account_id": ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
        "user_id": ttl_cleanup.SYSTEM_TASK_USER_ID,
        "target": {
            "object_type": object_type,
            "object_uri": object_uri,
            "account_id": "acct",
            "user_id": "u1",
            "session_id": session_id,
            "expires_at": "2020-01-01T00:00:00.000Z",
        },
    }


def _make_service(*, viking_fs, sessions=None, fs=None):
    return SimpleNamespace(viking_fs=viking_fs, sessions=sessions, fs=fs)


def _cleanup_service(monkeypatch, service, event_loop_enabled: bool = True):
    # TTL must read as ENABLED so hidden_by_ttl actually evaluates expiry.
    # hidden_by_ttl lives in core.ttl and calls ttl_enabled() there, so patch
    # the seam at its definition site (not just the ttl_cleanup re-export).
    monkeypatch.setattr(core_ttl, "ttl_enabled", lambda: True)
    monkeypatch.setattr(ttl_cleanup, "ttl_enabled", lambda: True)
    svc = ttl_cleanup.TTLCleanupService.__new__(ttl_cleanup.TTLCleanupService)
    svc._service = service
    return svc


@pytest.fixture
def tracker():
    t = TaskTracker(_TaskStore())
    set_task_tracker(t)
    try:
        yield t
    finally:
        set_task_tracker(None)


@pytest.mark.asyncio
async def test_expired_session_is_deleted_strictly(monkeypatch, tracker):
    viking_fs = SimpleNamespace(
        read_file=AsyncMock(return_value=_session_meta("2020-01-01T00:00:00.000Z"))
    )
    sessions = SimpleNamespace(delete=AsyncMock(return_value=True))
    svc = _cleanup_service(
        monkeypatch, _make_service(viking_fs=viking_fs, sessions=sessions)
    )

    error = await svc._process(
        _message(OBJECT_TYPE_SESSION, object_uri=SESSION_URI, session_id="s1")
    )

    assert error is None
    sessions.delete.assert_awaited_once()
    # Strict cleanup: billing only stops once vectors are confirmed gone.
    assert sessions.delete.await_args.kwargs["strict"] is True
    task = await tracker.get("task-1", account_id=ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
                             user_id=ttl_cleanup.SYSTEM_TASK_USER_ID)
    assert task.status == TaskStatus.COMPLETED
    assert task.result == {"deleted": True}


@pytest.mark.asyncio
async def test_expired_event_is_deleted_strictly(monkeypatch, tracker):
    viking_fs = SimpleNamespace(
        read_file=AsyncMock(return_value=_event_body("2020-01-01T00:00:00.000Z"))
    )
    fs = SimpleNamespace(rm=AsyncMock(return_value=None))
    svc = _cleanup_service(monkeypatch, _make_service(viking_fs=viking_fs, fs=fs))

    error = await svc._process(_message(OBJECT_TYPE_EVENT, object_uri=EVENT_URI))

    assert error is None
    fs.rm.assert_awaited_once()
    assert fs.rm.await_args.kwargs["strict"] is True


@pytest.mark.asyncio
async def test_renewed_session_is_not_deleted(monkeypatch, tracker):
    # Re-read shows a future expiry (renewed after being scheduled) -> skip.
    viking_fs = SimpleNamespace(
        read_file=AsyncMock(return_value=_session_meta("2999-01-01T00:00:00.000Z"))
    )
    sessions = SimpleNamespace(delete=AsyncMock())
    svc = _cleanup_service(
        monkeypatch, _make_service(viking_fs=viking_fs, sessions=sessions)
    )

    error = await svc._process(
        _message(OBJECT_TYPE_SESSION, object_uri=SESSION_URI, session_id="s1")
    )

    assert error is None
    sessions.delete.assert_not_awaited()
    task = await tracker.get("task-1", account_id=ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
                             user_id=ttl_cleanup.SYSTEM_TASK_USER_ID)
    assert task.result == {"deleted": False, "skipped": "not_expired"}


@pytest.mark.asyncio
async def test_ttl_disabled_between_scan_and_process_halts_delete(monkeypatch, tracker):
    # Config flipped OFF after scheduling -> hidden_by_ttl False -> no delete.
    viking_fs = SimpleNamespace(
        read_file=AsyncMock(return_value=_session_meta("2020-01-01T00:00:00.000Z"))
    )
    sessions = SimpleNamespace(delete=AsyncMock())
    svc = ttl_cleanup.TTLCleanupService.__new__(ttl_cleanup.TTLCleanupService)
    svc._service = _make_service(viking_fs=viking_fs, sessions=sessions)
    monkeypatch.setattr(ttl_cleanup, "ttl_enabled", lambda: False)

    error = await svc._process(
        _message(OBJECT_TYPE_SESSION, object_uri=SESSION_URI, session_id="s1")
    )

    assert error is None
    sessions.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_task_is_not_reprocessed(monkeypatch, tracker):
    # Simulate a re-delivered message whose task already completed.
    await tracker.create(
        "ttl_cleanup",
        resource_id=SESSION_URI,
        task_id="task-1",
        account_id=ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
        user_id=ttl_cleanup.SYSTEM_TASK_USER_ID,
    )
    await tracker.complete(
        "task-1", {"deleted": True},
        account_id=ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
        user_id=ttl_cleanup.SYSTEM_TASK_USER_ID,
    )
    viking_fs = SimpleNamespace(read_file=AsyncMock())
    sessions = SimpleNamespace(delete=AsyncMock())
    svc = _cleanup_service(
        monkeypatch, _make_service(viking_fs=viking_fs, sessions=sessions)
    )

    error = await svc._process(
        _message(OBJECT_TYPE_SESSION, object_uri=SESSION_URI, session_id="s1")
    )

    assert error is None
    viking_fs.read_file.assert_not_awaited()
    sessions.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_gone_is_success(monkeypatch, tracker):
    viking_fs = SimpleNamespace(
        read_file=AsyncMock(return_value=_session_meta("2020-01-01T00:00:00.000Z"))
    )
    sessions = SimpleNamespace(delete=AsyncMock(side_effect=NotFoundError("s1", "session")))
    svc = _cleanup_service(
        monkeypatch, _make_service(viking_fs=viking_fs, sessions=sessions)
    )

    error = await svc._process(
        _message(OBJECT_TYPE_SESSION, object_uri=SESSION_URI, session_id="s1")
    )

    assert error is None
    task = await tracker.get("task-1", account_id=ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
                             user_id=ttl_cleanup.SYSTEM_TASK_USER_ID)
    assert task.status == TaskStatus.COMPLETED
    assert task.result == {"deleted": True, "already_gone": True}


@pytest.mark.asyncio
async def test_delete_failure_marks_task_failed(monkeypatch, tracker):
    viking_fs = SimpleNamespace(
        read_file=AsyncMock(return_value=_session_meta("2020-01-01T00:00:00.000Z"))
    )
    sessions = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("vector residue")))
    svc = _cleanup_service(
        monkeypatch, _make_service(viking_fs=viking_fs, sessions=sessions)
    )

    error = await svc._process(
        _message(OBJECT_TYPE_SESSION, object_uri=SESSION_URI, session_id="s1")
    )

    assert error is not None
    assert "vector residue" in error
    task = await tracker.get("task-1", account_id=ttl_cleanup.SYSTEM_TASK_ACCOUNT_ID,
                             user_id=ttl_cleanup.SYSTEM_TASK_USER_ID)
    assert task.status == TaskStatus.FAILED
