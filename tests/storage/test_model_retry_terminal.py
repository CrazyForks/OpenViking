# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from openviking.storage.collection_schemas import TextEmbeddingHandler
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.storage.queuefs.process_result import ProcessOutcome
from openviking.storage.queuefs.semantic_msg import SemanticMsg
from openviking.storage.queuefs.semantic_processor import SemanticProcessor
from openviking.telemetry.request_wait_tracker import get_request_wait_tracker
from openviking.utils.circuit_breaker import CircuitBreakerOpen
from openviking.utils.model_call import ModelCallError, run_model_async


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,attempts",
    [
        ("429 TooManyRequests", 4),
        ("401 Unauthorized", 1),
        ("content safety", 1),
        ("unknown provider failure", 1),
    ],
)
async def test_embedding_terminal_failure_settles_wait_without_requeue(
    monkeypatch, message, attempts
):
    sent = 0

    class Embedder:
        def prepare_embedding_input(self, content):
            return content

        async def embed_async(self, content, is_query=False):
            async def once():
                nonlocal sent
                sent += 1
                raise RuntimeError(message)

            return await run_model_async(once, model_type="embedding")

    config = SimpleNamespace(
        storage=SimpleNamespace(vectordb=SimpleNamespace(name="context")),
        embedding=SimpleNamespace(
            dimension=2,
            max_input_tokens=8192,
            dense=None,
            sparse=None,
            hybrid=None,
            get_embedder=lambda: Embedder(),
            circuit_breaker=SimpleNamespace(
                failure_threshold=5, reset_timeout=60, max_reset_timeout=600
            ),
        ),
    )
    monkeypatch.setattr("openviking_cli.utils.config.get_openviking_config", lambda: config)
    monkeypatch.setattr("openviking.utils.model_call.random.uniform", lambda *_: 0)
    backend = SimpleNamespace(
        is_closing=False, has_queue_manager=True, enqueue_embedding_msg=AsyncMock()
    )
    handler = TextEmbeddingHandler(backend)
    msg = EmbeddingMsg(
        message="fixture",
        context_data={
            "id": "fixture",
            "uri": "viking://resources/fixture",
            "account_id": "fixture",
        },
        telemetry_id=uuid4().hex,
        model_operation="add_resource",
    )
    tracker = get_request_wait_tracker()
    tracker.register_request(msg.telemetry_id)
    tracker.register_embedding_root(msg.telemetry_id, msg.id)
    try:
        result = await handler.on_dequeue({"data": msg.to_json()})
        assert result.outcome == ProcessOutcome.FAILED
        assert sent == attempts
        backend.enqueue_embedding_msg.assert_not_awaited()
        await asyncio.wait_for(tracker.wait_for_request(msg.telemetry_id), timeout=1)
        status = tracker.build_queue_status(msg.telemetry_id)["Embedding"]
        assert status["error_count"] == 1
        assert status["requeue_count"] == 0
    finally:
        tracker.cleanup(msg.telemetry_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("breaker_open", [False, True])
async def test_semantic_terminal_failure_releases_owned_lock_and_settles_wait(
    monkeypatch, breaker_open
):
    lease = {"owner_id": "consumer", "lease_ref": "fixture"}
    agfs = SimpleNamespace(
        pathlock_adopt=AsyncMock(return_value=lease), pathlock_release=AsyncMock()
    )
    fs = SimpleNamespace(_async_agfs=agfs, exists=AsyncMock(return_value=True))
    monkeypatch.setattr("openviking.storage.queuefs.semantic_processor.get_viking_fs", lambda: fs)
    close = AsyncMock()
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_processor.SemanticLockScope.resolve",
        AsyncMock(return_value=SimpleNamespace(lock=lease, close=close)),
    )
    processor = SemanticProcessor()
    processor._reenqueue_semantic_msg = AsyncMock()
    processor._cleanup_local_artifact = AsyncMock()
    if breaker_open:

        def reject():
            raise CircuitBreakerOpen("open")

        monkeypatch.setattr(processor._circuit_breaker, "check", reject)
    else:

        async def run(*args, **kwargs):
            raise ModelCallError("max_attempts", "transient", 4, "fixture")

        executor = SimpleNamespace(run=run)
        monkeypatch.setattr(
            "openviking.storage.queuefs.semantic_processor.SemanticTreeExecutor",
            lambda **kwargs: executor,
        )
    msg = SemanticMsg(
        uri="viking://resources/fixture",
        context_type="resource",
        telemetry_id=uuid4().hex,
        model_operation="add_resource",
        lock_handoff={"owner_id": "producer"},
        propagate_to_parent=False,
    )
    tracker = get_request_wait_tracker()
    tracker.register_request(msg.telemetry_id)
    tracker.register_semantic_root(msg.telemetry_id, msg.id)
    try:
        result = await processor.on_dequeue(msg.to_dict())
        assert result.outcome == ProcessOutcome.FAILED
        processor._reenqueue_semantic_msg.assert_not_awaited()
        if breaker_open:
            agfs.pathlock_adopt.assert_awaited_once_with(msg.lock_handoff)
            agfs.pathlock_release.assert_awaited_once_with(lease)
        else:
            close.assert_awaited_once()
        await asyncio.wait_for(tracker.wait_for_request(msg.telemetry_id), timeout=1)
        status = tracker.build_queue_status(msg.telemetry_id)["Semantic"]
        assert status["error_count"] == 1
        assert status["requeue_count"] == 0
    finally:
        tracker.cleanup(msg.telemetry_id)
