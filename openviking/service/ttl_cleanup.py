# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Background physical cleanup of TTL-expired events and sessions.

Logical invisibility (the read barrier in :mod:`openviking.core.ttl`) hides an
object the instant ``expires_at`` passes. This module is the *separate* physical
half: a periodic scanner finds objects whose frozen ``expires_at`` is in the
past and hands each one to a single-consumer ``TTL_CLEANUP`` queue. The queue
processor deletes the object *strictly* — it only marks the cleanup task
complete once files **and** vector index records are confirmed gone, which is
also the point at which the object stops counting toward billing (billing is
driven by the barrier-covered stat count, so it drops only after the physical
vectors are removed).

The design mirrors the existing durable-work pairs in the codebase:

- the scanner mirrors :class:`SessionAutoCommitScheduler` (an interval loop that
  walks ``/local`` and schedules work), and
- the processor mirrors ``_DeletionProcessor`` / ``setup_deletion`` (a
  ``DequeueHandlerBase`` dispatched on the owner loop, restart-safe via QueueFS
  RecoverStale + TaskTracker).

Everything here is inert while TTL is disabled (the default): the scan loop
still wakes on its interval but returns immediately when ``ttl_enabled()`` is
False, so no metadata is read and nothing is ever scheduled.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from openviking.core.ttl import (
    OBJECT_TYPE_EVENT,
    OBJECT_TYPE_SESSION,
    hidden_by_ttl,
    is_expired,
    ttl_enabled,
)
from openviking.pyagfs import AsyncAGFSClient
from openviking.server.error_mapping import is_not_found_error
from openviking.server.identity import RequestContext, Role
from openviking.service.task_store import SYSTEM_TASK_ACCOUNT_ID, SYSTEM_TASK_USER_ID
from openviking.service.task_tracker import TaskStatus, get_task_tracker
from openviking.service.task_tracker_concurrency import OwnerLoopDispatcher, run_to_completion
from openviking.session.memory.utils.messages import parse_memory_file_with_fields
from openviking.storage.queuefs.named_queue import DequeueHandlerBase
from openviking.storage.queuefs.process_result import ProcessResult
from openviking_cli.exceptions import NotFoundError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

AGFS_SCAN_ROOT = "/local"
SESSION_META_SUFFIX = "/.meta.json"
EVENT_FILE_SUFFIX = ".md"
_EVENTS_SEGMENT = "memories/events"

_TERMINAL_TASK_STATUSES = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)


@dataclass(frozen=True)
class _TTLCandidate:
    """A single in-scope object the scanner may need to clean up.

    ``object_uri`` is the canonical Viking URI the cleanup deletes; ``read_path``
    is the raw AGFS path the scanner reads to resolve the *live* frozen expiry
    (an event's own ``.md`` file, or a session's ``.meta.json``). ``session_id``
    is set only for sessions, routing the delete through ``SessionService``.
    """

    object_type: str
    object_uri: str
    read_path: str
    account_id: str
    user_id: str
    session_id: Optional[str] = None


def _ttl_cleanup_message(*, task_id: str, candidate: _TTLCandidate, expires_at: str) -> dict[str, Any]:
    """Build a durable TTL cleanup message.

    Owner (top-level ``account_id``/``user_id``) is the system identity so the
    task belongs to the cluster, matching the deletion queue. ``target`` carries
    the data owner needed to rebuild the deletion context, plus the scan-time
    ``expires_at`` snapshot for telemetry only — the authoritative expiry
    decision is re-made against the live object at process time.
    """
    return {
        "task_id": task_id,
        "account_id": SYSTEM_TASK_ACCOUNT_ID,
        "user_id": SYSTEM_TASK_USER_ID,
        "target": {
            "object_type": candidate.object_type,
            "object_uri": candidate.object_uri,
            "account_id": candidate.account_id,
            "user_id": candidate.user_id,
            "session_id": candidate.session_id,
            "expires_at": expires_at,
        },
    }


class TTLCleanupService:
    """Own the TTL cleanup queue consumer and the expiry scanner."""

    def __init__(
        self,
        *,
        service: Any,
        service_loop: asyncio.AbstractEventLoop,
        check_interval: Optional[float] = None,
    ) -> None:
        self._service = service
        self._service_loop = service_loop
        self._scheduler = TTLCleanupScheduler(service, check_interval=check_interval)

    async def initialize(self) -> None:
        """Bind the queue consumer and start the scanner.

        Restart safety comes from the standard QueueFS + TaskTracker path:
        ``QueueManager.prepare_task_tracking`` (run in core initialize) has
        already rebuilt work and restored task records from any recovered
        TTL_CLEANUP messages before this consumer is bound, so re-delivered
        messages resume cleanly here.
        """
        queue_manager = self._service._queue_manager
        queue = queue_manager.get_queue(queue_manager.TTL_CLEANUP)
        queue.set_dequeue_handler(_TTLCleanupProcessor(self, self._service_loop))
        await self._scheduler.start()

    async def close(self) -> None:
        await self._scheduler.stop()

    async def _process(self, message: dict[str, Any]) -> Optional[str]:
        """Delete one expired object, or no-op if it was renewed/replaced/gone.

        Returns an error string on failure (after marking the task failed) and
        ``None`` on success, mirroring ``_DeletionProcessor``. A failed cleanup
        is acked and the object is retried on the next scan cycle.
        """
        task_id = message["task_id"]
        owner = {"account_id": message["account_id"], "user_id": message["user_id"]}
        target = message["target"]
        object_type = target["object_type"]
        object_uri = target["object_uri"]
        data_account = target["account_id"]
        data_user = target["user_id"]
        session_id = target.get("session_id")

        tracker = get_task_tracker()
        # Idempotent get-or-create keyed by task_id. A re-delivered message after
        # restart resolves to the same record; a terminal record means this work
        # already settled and must not run again.
        task = await tracker.create(
            "ttl_cleanup",
            resource_id=object_uri,
            task_id=task_id,
            **owner,
        )
        if task.status in _TERMINAL_TASK_STATUSES:
            return None

        ctx = RequestContext(
            user=UserIdentifier(data_account, data_user or SYSTEM_TASK_USER_ID),
            role=Role.ROOT,
        )
        await tracker.start(task_id, **owner)
        try:
            # Renewal-wins / don't-delete-a-new-object: re-read the *live* frozen
            # expiry now, immediately before deleting. A session committed after
            # being scheduled carries a fresh expiry (renewed from its own frozen
            # ttl_days); a new object that reused the URI carries its own future
            # expiry. Either way the live object is no longer expired, so we skip
            # and settle the task as a no-op. Disabling TTL between scan and now
            # also lands here (hidden_by_ttl is False when TTL is off), halting
            # cleanup so data is never destroyed while the feature is off.
            still_expired = await run_to_completion(
                lambda: self._still_expired(object_type, object_uri, session_id, ctx)
            )
            if not still_expired:
                await tracker.complete(
                    task_id, {"deleted": False, "skipped": "not_expired"}, **owner
                )
                return None

            if object_type == OBJECT_TYPE_SESSION:
                await run_to_completion(
                    lambda: self._service.sessions.delete(session_id, ctx, strict=True)
                )
            else:
                await run_to_completion(
                    lambda: self._service.fs.rm(object_uri, ctx, recursive=False, strict=True)
                )
        except NotFoundError:
            # Already physically gone (idempotent re-delivery or a racing
            # interactive delete). Strict cleanup goal is satisfied.
            await tracker.complete(task_id, {"deleted": True, "already_gone": True}, **owner)
            return None
        except Exception as exc:
            logger.exception("TTL cleanup failed for %s", object_uri)
            error = f"TTL cleanup failed: {exc}"
            await tracker.fail(task_id, error, **owner)
            return error

        await tracker.complete(task_id, {"deleted": True}, **owner)
        return None

    async def _still_expired(
        self,
        object_type: str,
        object_uri: str,
        session_id: Optional[str],
        ctx: RequestContext,
    ) -> bool:
        """Whether the live object is currently logically expired.

        Fail-safe: an object we cannot read or prove expired is treated as *not*
        expired so cleanup never destroys data on a transient read error — the
        next scan retries. Gated through ``hidden_by_ttl`` so a disabled TTL
        config yields False and stops all deletion.
        """
        viking_fs = self._service.viking_fs
        read_uri = f"{object_uri}{SESSION_META_SUFFIX}" if object_type == OBJECT_TYPE_SESSION else object_uri
        try:
            content = await viking_fs.read_file(read_uri, ctx=ctx)
        except NotFoundError:
            return False
        except Exception:
            logger.debug("TTL re-check could not read %s", read_uri, exc_info=True)
            return False
        if object_type == OBJECT_TYPE_SESSION:
            try:
                expires_at = json.loads(content).get("expires_at", "")
            except (json.JSONDecodeError, TypeError, ValueError):
                return False
        else:
            expires_at = parse_memory_file_with_fields(content).get("expires_at", "")
        return hidden_by_ttl(expires_at)


class TTLCleanupScheduler:
    """Interval scanner that schedules cleanup for expired events and sessions.

    Mirrors :class:`SessionAutoCommitScheduler`: an ``asyncio`` loop that wakes
    every ``check_interval`` seconds and, only while TTL is enabled, walks the
    ``/local`` tree to find objects whose frozen ``expires_at`` is in the past.
    Each due object is handed to the TTL_CLEANUP queue, de-duplicated against
    messages already in flight so a slow or failed cleanup is not enqueued
    twice. TTL is day-granularity, so the default interval is coarse (hourly);
    an operator never needs minute precision here.
    """

    DEFAULT_CHECK_INTERVAL = 3600.0
    DEFAULT_SCAN_BATCH_SIZE = 64

    def __init__(
        self,
        service: Any,
        *,
        check_interval: Optional[float] = None,
        scan_batch_size: int = DEFAULT_SCAN_BATCH_SIZE,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._service = service
        self._check_interval = (
            self.DEFAULT_CHECK_INTERVAL if check_interval is None else float(check_interval)
        )
        self._scan_batch_size = max(1, int(scan_batch_size))
        self._sleep = sleep
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._agfs_client: Optional[AsyncAGFSClient] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        logger.info("TTLCleanupScheduler started with check interval %.3fs", self._check_interval)
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            try:
                # Default-off short circuit: no metadata reads, no scheduling.
                if ttl_enabled():
                    await self._scan_once()
            except Exception as exc:
                logger.error("TTL cleanup scheduler loop failed: %s", exc, exc_info=True)

    async def _scan_once(self) -> None:
        now = datetime.now(timezone.utc)
        agfs = self._get_agfs_client()
        queue_manager = self._service._queue_manager
        queue = queue_manager.get_queue(queue_manager.TTL_CLEANUP)
        # De-dupe against work already in flight so a slow/failed cleanup for an
        # object is not enqueued again while its message is still pending.
        in_flight = await self._queued_object_uris(queue)
        scanned = 0
        scheduled = 0
        async for batch in self._iter_candidate_batches(agfs):
            scanned += len(batch)
            expiries = await asyncio.gather(
                *(self._read_expiry(agfs, candidate) for candidate in batch)
            )
            for candidate, expires_at in zip(batch, expiries):
                if not is_expired(expires_at, now=now):
                    continue
                if candidate.object_uri in in_flight:
                    continue
                await queue.enqueue(
                    _ttl_cleanup_message(
                        task_id=str(uuid4()),
                        candidate=candidate,
                        expires_at=expires_at or "",
                    )
                )
                in_flight.add(candidate.object_uri)
                scheduled += 1
        if scheduled:
            logger.info("TTLCleanupScheduler scanned=%d scheduled=%d", scanned, scheduled)

    def _get_agfs_client(self) -> AsyncAGFSClient:
        if self._agfs_client is None:
            self._agfs_client = AsyncAGFSClient(self._service.viking_fs.agfs)
        return self._agfs_client

    async def _queued_object_uris(self, queue: Any) -> set[str]:
        uris: set[str] = set()
        try:
            messages = await queue.snapshot()
        except Exception:
            logger.debug("TTL cleanup queue snapshot failed", exc_info=True)
            return uris
        for message in messages:
            payload = message.get("data", message) if isinstance(message, dict) else None
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    continue
            if not isinstance(payload, dict):
                continue
            target = payload.get("target")
            uri = target.get("object_uri") if isinstance(target, dict) else None
            if uri:
                uris.add(str(uri))
        return uris

    async def _read_expiry(self, agfs: AsyncAGFSClient, candidate: _TTLCandidate) -> str:
        """Read a candidate's frozen ``expires_at``, or ``""`` when unavailable.

        Uses raw AGFS reads (like the idle session scanner) to avoid per-object
        access checks during a system-owned sweep. An empty string means "no
        provable expiry", which ``is_expired`` treats as not-expired.
        """
        try:
            content = await agfs.read(candidate.read_path)
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.debug("TTL scan could not read %s", candidate.read_path, exc_info=True)
            return ""
        raw = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        if candidate.object_type == OBJECT_TYPE_SESSION:
            try:
                meta = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                return ""
            return str(meta.get("expires_at", "")) if isinstance(meta, dict) else ""
        return str(parse_memory_file_with_fields(raw).get("expires_at", "") or "")

    async def _iter_candidate_batches(
        self, agfs: AsyncAGFSClient
    ) -> AsyncIterator[list[_TTLCandidate]]:
        """Yield fixed-size batches of in-scope candidates across all users."""
        try:
            account_entries = await agfs.ls(AGFS_SCAN_ROOT)
        except Exception:
            logger.warning("TTL cleanup failed to scan AGFS tree", exc_info=True)
            return

        batch: list[_TTLCandidate] = []
        for account_entry in account_entries:
            account_id = str(account_entry.get("name") or "").strip()
            if not account_id or account_id == "_system":
                continue
            if not account_entry.get("isDir", False):
                continue
            user_root = f"{AGFS_SCAN_ROOT}/{account_id}/user"
            for user_entry in await self._ls_or_empty(agfs, user_root):
                user_id = str(user_entry.get("name") or "").strip()
                if not user_id or not user_entry.get("isDir", False):
                    continue
                async for candidate in self._iter_user_candidates(agfs, account_id, user_id):
                    batch.append(candidate)
                    if len(batch) >= self._scan_batch_size:
                        yield batch
                        batch = []
        if batch:
            yield batch

    async def _iter_user_candidates(
        self, agfs: AsyncAGFSClient, account_id: str, user_id: str
    ) -> AsyncIterator[_TTLCandidate]:
        base_path = f"{AGFS_SCAN_ROOT}/{account_id}/user/{user_id}"
        base_uri = f"viking://user/{user_id}"

        # Sessions: viking://user/{uid}/sessions/{sid}
        sessions_path = f"{base_path}/sessions"
        sessions_uri = f"{base_uri}/sessions"
        for entry in await self._ls_or_empty(agfs, sessions_path):
            session_id = str(entry.get("name") or "").strip()
            if not session_id or not entry.get("isDir", False):
                continue
            yield _TTLCandidate(
                object_type=OBJECT_TYPE_SESSION,
                object_uri=f"{sessions_uri}/{session_id}",
                read_path=f"{sessions_path}/{session_id}{SESSION_META_SUFFIX}",
                account_id=account_id,
                user_id=user_id,
                session_id=session_id,
            )

        # User events: viking://user/{uid}/memories/events/...
        async for candidate in self._iter_event_candidates(
            agfs,
            events_path=f"{base_path}/{_EVENTS_SEGMENT}",
            events_uri=f"{base_uri}/{_EVENTS_SEGMENT}",
            account_id=account_id,
            user_id=user_id,
        ):
            yield candidate

        # Peer events: viking://user/{uid}/peers/{pid}/memories/events/...
        peers_path = f"{base_path}/peers"
        for entry in await self._ls_or_empty(agfs, peers_path):
            peer_id = str(entry.get("name") or "").strip()
            if not peer_id or not entry.get("isDir", False):
                continue
            async for candidate in self._iter_event_candidates(
                agfs,
                events_path=f"{peers_path}/{peer_id}/{_EVENTS_SEGMENT}",
                events_uri=f"{base_uri}/peers/{peer_id}/{_EVENTS_SEGMENT}",
                account_id=account_id,
                user_id=user_id,
            ):
                yield candidate

    async def _iter_event_candidates(
        self,
        agfs: AsyncAGFSClient,
        *,
        events_path: str,
        events_uri: str,
        account_id: str,
        user_id: str,
    ) -> AsyncIterator[_TTLCandidate]:
        try:
            entries = await agfs.tree_directory(events_path, show_hidden=False)
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.debug("TTL scan could not walk %s", events_path, exc_info=True)
            return
        for entry in entries:
            info = entry.get("info") if isinstance(entry, dict) else None
            if isinstance(info, dict) and info.get("isDir", False):
                continue
            path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
            if not path or not path.endswith(EVENT_FILE_SUFFIX):
                continue
            rel = path[len(events_path):].lstrip("/")
            if not rel:
                continue
            yield _TTLCandidate(
                object_type=OBJECT_TYPE_EVENT,
                object_uri=f"{events_uri}/{rel}",
                read_path=path,
                account_id=account_id,
                user_id=user_id,
                session_id=None,
            )

    async def _ls_or_empty(self, agfs: AsyncAGFSClient, path: str) -> list[dict[str, Any]]:
        try:
            return await agfs.ls(path)
        except Exception as exc:
            if not is_not_found_error(exc):
                logger.debug("TTL scan could not list %s", path, exc_info=True)
            return []


class _TTLCleanupProcessor(DequeueHandlerBase):
    """Single-consumer handler that runs one cleanup on the owner loop."""

    def __init__(
        self,
        cleanup_service: TTLCleanupService,
        service_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._cleanup_service = cleanup_service
        self._dispatcher = OwnerLoopDispatcher(service_loop)

    @staticmethod
    def _parse_message(data: dict[str, Any]) -> dict[str, Any]:
        payload = data.get("data", data)
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("Invalid TTL cleanup message")
        target = payload.get("target")
        if not isinstance(target, dict):
            raise ValueError("Invalid TTL cleanup target")
        if not payload.get("task_id") or not payload.get("account_id") or not payload.get("user_id"):
            raise ValueError("Invalid TTL cleanup owner")
        object_type = target.get("object_type")
        object_uri = target.get("object_uri")
        if object_type not in (OBJECT_TYPE_EVENT, OBJECT_TYPE_SESSION) or not object_uri:
            raise ValueError("Invalid TTL cleanup object")
        if object_type == OBJECT_TYPE_SESSION and not target.get("session_id"):
            raise ValueError("Invalid TTL cleanup session")
        return {
            "task_id": str(payload["task_id"]),
            "account_id": str(payload["account_id"]),
            "user_id": str(payload["user_id"]),
            "target": {
                "object_type": str(object_type),
                "object_uri": str(object_uri),
                "account_id": str(target.get("account_id") or ""),
                "user_id": str(target.get("user_id") or ""),
                "session_id": (
                    str(target["session_id"]) if target.get("session_id") else None
                ),
                "expires_at": str(target.get("expires_at") or ""),
            },
        }

    async def on_dequeue(self, data: Optional[dict[str, Any]]) -> ProcessResult:
        if not data:
            return ProcessResult.success()
        try:
            message = self._parse_message(data)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return ProcessResult.failed(str(exc))
        error = await self._dispatcher.run(lambda: self._cleanup_service._process(message))
        return ProcessResult.success() if error is None else ProcessResult.failed(error)


async def setup_ttl_cleanup(*, service: Any) -> Optional[TTLCleanupService]:
    """Create the TTL cleanup service once storage and the queue are ready.

    Returns ``None`` when the queue manager or storage is unavailable. The
    service itself is inert while TTL is disabled — the scanner short-circuits
    on ``ttl_enabled()`` — so it is always safe to construct.
    """
    if service.viking_fs is None or service._queue_manager is None:
        return None
    cleanup_service = TTLCleanupService(
        service=service,
        service_loop=asyncio.get_running_loop(),
    )
    await cleanup_service.initialize()
    # Bind onto the core service so OpenVikingService.close() stops the scanner
    # alongside the other schedulers during shutdown.
    service._ttl_cleanup_service = cleanup_service
    return cleanup_service
