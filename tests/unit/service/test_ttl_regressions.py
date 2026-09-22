"""Regression coverage for TTL deletion, recovery, visibility and retry boundaries."""

import json
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from openviking.core import ttl
from openviking.server.identity import RequestContext, Role
from openviking.service.session_service import SessionService
from openviking.service.task_tracker import TaskTracker, set_task_tracker
from openviking.session.session import Session
from openviking.storage.queuefs.queue_manager import QueueManager
from openviking.storage.viking_fs import VikingFS
from openviking_cli.exceptions import AlreadyExistsError, NotFoundError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.ttl_config import TTLConfig
from tests.unit.service.test_ttl_cleanup import (
    _make_service,
    _message,
    _record,
    _session_meta,
    _TaskStore,
)
from tests.unit.session.test_session_commit_resume import _MemoryVikingFS


def _default_ctx():
    return RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)


class _DummyAgfs:
    def stat(self, path, ctx=None):
        return {}


@pytest.mark.parametrize(
    "parent_policy,expected",
    [
        ({"mode": "days", "ttl_days": 90}, 90),
        ({"mode": "disabled"}, None),
    ],
)
def test_inherit_preserves_nearest_explicit_ancestor(parent_policy, expected):
    parent = "viking://user/u1/memories/events"
    config = TTLConfig.model_validate(
        {
            "global": {"mode": "days", "ttl_days": 1},
            "directories": {parent: parent_policy, parent + "/2026": {"mode": "inherit"}},
        }
    )
    actual = ttl.resolve_ttl_days(parent + "/2026/e.md", config)
    assert actual == expected, f"parent={parent_policy}; expected {expected}; got {actual}"


@pytest.mark.asyncio
async def test_expired_event_is_not_exposed_through_parent_abstract(monkeypatch):
    fs = VikingFS(agfs=_DummyAgfs())
    ctx = _default_ctx()
    parent = "viking://user/default/memories/events/2026"
    event = parent + "/e.md"
    parent_path = fs._uri_to_path(parent, ctx=ctx)
    event_path = fs._uri_to_path(event, ctx=ctx)
    secret = "expired-event-only-secret"
    files = {
        event_path: (
            '<!-- MEMORY_FIELDS {"expires_at":"2000-01-01T00:00:00.000Z"} -->\n' + secret
        ).encode(),
        parent_path + "/.abstract.md": ("Summary: " + secret).encode(),
    }

    async def stat(path):
        if path == parent_path:
            return {"name": "2026", "isDir": True}
        if path in files:
            return {"name": path.rsplit("/", 1)[-1], "isDir": False}
        raise FileNotFoundError(path)

    monkeypatch.setattr(fs._async_agfs, "stat", stat)
    monkeypatch.setattr(fs._async_agfs, "read", AsyncMock(side_effect=lambda path: files[path]))
    monkeypatch.setattr(fs.ttl_registry, "account_may_have_records", AsyncMock(return_value=True))
    monkeypatch.setattr(fs.ttl_registry, "summary_requires_snapshot", AsyncMock(return_value=True))
    with pytest.raises(NotFoundError):
        await fs.read_file(event, ctx=ctx)
    summary = await fs.abstract(parent, ctx=ctx)
    assert secret not in summary, f"event read is hidden but parent abstract returns: {summary}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expiry,visible",
    [
        ("2000-01-01T00:00:00.000Z", False),
        ("2999-01-01T00:00:00.000Z", True),
    ],
)
async def test_summary_deadline_applies_to_all_public_read_forms(monkeypatch, expiry, visible):
    from openviking.storage.abstract_overview import render_abstract_overview

    fs = VikingFS(agfs=_DummyAgfs())
    ctx = _default_ctx()
    parent = "viking://user/default/memories/events/2026"
    path = fs._uri_to_path(parent, ctx=ctx)
    files = {
        path + "/.abstract.md": render_abstract_overview(
            0, parent, "secret", {"expires_at": expiry}
        ).encode(),
        path + "/.overview.md": render_abstract_overview(
            1, parent, "secret", {"expires_at": expiry}
        ).encode(),
    }

    async def stat(candidate):
        if candidate == path:
            return {"name": "2026", "isDir": True}
        if candidate in files:
            return {"name": candidate.rsplit("/", 1)[-1], "isDir": False}
        raise FileNotFoundError(candidate)

    monkeypatch.setattr(fs._async_agfs, "stat", stat)
    monkeypatch.setattr(
        fs._async_agfs, "read", AsyncMock(side_effect=lambda candidate: files[candidate])
    )
    monkeypatch.setattr(fs.ttl_registry, "account_may_have_records", AsyncMock(return_value=True))
    monkeypatch.setattr(fs.ttl_registry, "summary_requires_snapshot", AsyncMock(return_value=True))
    assert ("secret" in await fs.abstract(parent, ctx=ctx)) is visible
    assert ("secret" in await fs.overview(parent, ctx=ctx)) is visible
    for filename in (".abstract.md", ".overview.md"):
        for read in (fs.read_file, fs.read_file_bytes):
            if visible:
                assert await read(parent + "/" + filename, ctx=ctx)
            else:
                with pytest.raises(NotFoundError):
                    await read(parent + "/" + filename, ctx=ctx)


class ProcessCrash(BaseException):
    pass


@pytest.mark.asyncio
async def test_cleanup_reconciles_persisted_phase2_completion_after_crash(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 21, 23, 59, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(ttl, "datetime", Clock)
    monkeypatch.setattr(
        "openviking.session.session.get_current_timestamp", lambda: "2026-09-21T23:59:00.000Z"
    )
    uri = "viking://user/u1/sessions/s1"
    archive = uri + "/history/archive_001"
    old_expiry = "2026-09-22T00:00:00.000Z"
    files = {
        uri + "/messages.jsonl": "",
        uri + "/.meta.json": json.dumps(
            {
                "session_id": "s1",
                "ttl_days": 2,
                "received_at": "2026-09-20T00:00:00.000Z",
                "expires_at": old_expiry,
                "ttl_generation": "generation-1",
            }
        ),
    }
    storage = _MemoryVikingFS(files)
    session = Session(viking_fs=storage, session_id="s1", session_uri=uri)
    await session.load(include_expired=True)
    write = storage.write_file

    async def crash_on_root_save(uri, content, ctx=None, lease_ref=None):
        if uri.endswith("/sessions/s1/.meta.json"):
            raise ProcessCrash("termination after durable archive completion, before root renewal")
        await write(uri, content, ctx=ctx, lease_ref=lease_ref)

    storage.write_file = crash_on_root_save
    with pytest.raises(ProcessCrash):
        await session._merge_and_save_commit_meta(
            archive_uri=archive,
            archive_index=1,
            memories_extracted={},
            telemetry_snapshot=None,
            ttl_generation="generation-1",
        )
    assert (
        json.loads(files[archive + "/.meta.json"])["phase2_completed_at"]
        == "2026-09-21T23:59:00.000Z"
    )
    assert json.loads(files[uri + "/.meta.json"])["expires_at"] == old_expiry
    Clock.current = datetime(2026, 9, 22, 0, 0, 1, tzinfo=timezone.utc)
    record = _record(object_uri=uri, expires_at=old_expiry)
    cleanup, vfs, registry, _ = _make_service(
        record=record, live_content=files[uri + "/.meta.json"]
    )
    vfs.read_file = storage.read_file
    vfs.write_file = write

    async def list_history(path, **kwargs):
        return await storage.ls(uri + "/history")

    vfs._async_agfs.ls = list_history
    result = await cleanup._cleanup_record(record)
    assert not result["deleted"], (
        "Phase2 completed at 2026-09-21 23:59 and should renew to 2026-09-23 23:59; "
        f"cleanup instead issued strict recursive rm: {vfs.rm.await_args}"
    )


def test_failed_cleanup_does_not_reconsume_without_any_wait():
    record = _record()
    cleanup, vfs, _, queue_manager = _make_service(
        record=record,
        live_content=_session_meta(),
        rm_error=RuntimeError("vector backend unavailable"),
    )
    pending = [_message(record)]
    tracker = TaskTracker(_TaskStore())
    set_task_tracker(tracker)

    class StopEvent(threading.Event):
        waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            return super().wait(0)

    stop = StopEvent()
    attempts = []

    async def enqueue(name, message):
        pending.append(message)

    queue_manager.enqueue.side_effect = enqueue

    class Queue:
        name = QueueManager.TTL_CLEANUP

        def has_dequeue_handler(self):
            return True

        async def size(self):
            if not pending:
                stop.set()
            return len(pending)

        async def dequeue(self):
            message = pending.pop(0)
            attempts.append(message["retry_count"])
            await cleanup._process(message)
            if len(attempts) == 5:
                stop.set()
            return message

    manager = QueueManager.__new__(QueueManager)
    manager._poll_interval = 0.1
    try:
        manager._queue_worker_loop(Queue(), stop, 1)
    finally:
        set_task_tracker(None)
    assert attempts == [0], (
        "failed work must leave the immediate queue until its durable retry time"
    )
    vfs.ttl_registry.defer_retry.assert_awaited_once()
    assert stop.waits


@pytest.mark.asyncio
async def test_create_does_not_report_success_for_invisible_expired_session(monkeypatch):
    fs = VikingFS(agfs=_DummyAgfs())
    ctx = _default_ctx()
    uri = "viking://user/default/sessions/s1"
    path = fs._uri_to_path(uri, ctx=ctx)
    files = {
        path
        + "/.meta.json": b'{"session_id":"s1","expires_at":"2000-01-01T00:00:00.000Z","ttl_generation":"old"}'
    }

    async def stat(candidate):
        if candidate == path:
            return {"name": "s1", "isDir": True}
        if candidate in files:
            return {"name": candidate.rsplit("/", 1)[-1], "isDir": False}
        raise FileNotFoundError(candidate)

    monkeypatch.setattr(fs._async_agfs, "stat", stat)
    monkeypatch.setattr(
        fs._async_agfs, "read", AsyncMock(side_effect=lambda candidate: files[candidate])
    )
    monkeypatch.setattr(fs.ttl_registry, "account_may_have_records", AsyncMock(return_value=True))
    monkeypatch.setattr(fs.ttl_registry, "summary_requires_snapshot", AsyncMock(return_value=True))
    service = SessionService.__new__(SessionService)
    service._record_lifecycle_metric = lambda *args: None
    service._new_session_auto_commit_policy = lambda: None
    service.session = lambda context, sid: Session(
        viking_fs=fs, ctx=context, session_id=sid, session_uri=uri
    )
    with pytest.raises(AlreadyExistsError):
        await service.create(ctx, session_id="s1")
