"""Offline fault injection against real OV adapters and a single-owner prototype.

All HTTP traffic is intercepted by httpx.MockTransport. No real credentials,
model requests, live QueueFS, token billing or production writes are involved.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import inspect
import json
import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from prototype import ModelFailure, ModelRetryOwner, RetryContext
from volcenginesdkarkruntime import AsyncArk

from openviking.models.embedder.volcengine_embedders import VolcengineDenseEmbedder
from openviking.models.vlm.backends.volcengine_vlm import VolcEngineVLM
from openviking.models.vlm.base import MultiCredentialVLM
from openviking.storage.collection_schemas import TextEmbeddingHandler
from openviking.storage.queuefs.embedding_msg import EmbeddingMsg
from openviking.utils.model_retry import classify_api_error, retry_async

_MEMORY_EXTRACTION_MAX_RETRIES = 3  # Historical policy at the pinned baseline.
BASELINE = "611f5c469b2bb8dc6d072b215251379e780d3f23"


class Transport:
    def __init__(self, status=429, code="TooManyRequests"):
        self.status, self.code, self.requests = status, code, 0

    def __call__(self, request):
        self.requests += 1
        return httpx.Response(
            self.status,
            json={"error": {"code": self.code, "message": self.code}},
            request=request,
        )

    def client(self, **kwargs):
        # Omitting max_retries deliberately preserves the installed SDK default.
        client = AsyncArk(
            api_key="offline-fixture",
            base_url="https://fixture.invalid/api/v3",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self)),
            **kwargs,
        )
        client._calculate_retry_timeout = lambda *a, **kw: 0.0
        return client


async def expect_failure(call):
    try:
        await call()
    except Exception:
        return
    raise AssertionError("fault injection unexpectedly succeeded")


async def embedding_baseline():
    transport = Transport()
    client = transport.client()
    embedder = VolcengineDenseEmbedder(
        model_name="fixture",
        api_key="offline-fixture",
        dimension=2,
        input_type="text",
        config={"max_retries": 3},
    )
    embedder._get_async_client = lambda: client
    try:
        await expect_failure(lambda: embedder.embed_async("fixture"))
        return {
            "scenario": "embedding_429_one_delivery",
            "http_requests": transport.requests,
            "ov_max_retries": embedder.max_retries,
            "sdk_max_retries": client.max_retries,
        }
    finally:
        await client.close()
        embedder.client.close()


async def vlm_baseline(status=429, code="TooManyRequests", outer=False, credentials=1):
    transport = Transport(status, code)
    client = transport.client(max_retries=0)
    models = []
    for _ in range(credentials):
        model = VolcEngineVLM({"model": "fixture", "api_key": "offline-fixture", "max_retries": 3})
        model.get_async_client = lambda: client
        models.append(model)
    model = (
        models[0]
        if credentials == 1
        else MultiCredentialVLM(models, [f"c-{i}" for i in range(credentials)])
    )

    async def call():
        return await model.get_completion_async("fixture")

    try:
        # Compose the same shared retry_async helper and max_retries used by
        # Session._run_retryable_phase2_step. This is not a full Session execution.
        await expect_failure(
            (lambda: retry_async(call, max_retries=_MEMORY_EXTRACTION_MAX_RETRIES, base_delay=0))
            if outer
            else call
        )
        return {
            "scenario": f"vlm_{status}_outer_{outer}_credentials_{credentials}",
            "http_requests": transport.requests,
        }
    finally:
        await client.close()


async def queue_baseline(kind, deliveries=3):
    # Real TextEmbeddingHandler and serializer; fake DB/queue and model boundary.
    # Deliberately cap the experiment: observing three deliveries is not a test
    # that infinity was executed. Unboundedness follows from the source branch.
    messages = []
    calls = 0

    class Embedder:
        def prepare_embedding_input(self, content):
            return content

        async def embed_async(self, content, is_query=False):
            nonlocal calls
            calls += 1
            raise RuntimeError(kind)

    async def enqueue(msg):
        messages.append(msg)

    backend = SimpleNamespace(
        is_closing=False, has_queue_manager=True, enqueue_embedding_msg=enqueue
    )
    config = SimpleNamespace(
        storage=SimpleNamespace(vectordb=SimpleNamespace(name="context")),
        embedding=SimpleNamespace(
            dimension=2,
            circuit_breaker=SimpleNamespace(
                failure_threshold=5, reset_timeout=60, max_reset_timeout=600
            ),
        ),
    )
    with patch("openviking_cli.utils.config.get_openviking_config", return_value=config):
        handler = TextEmbeddingHandler(backend)
    handler._embedder = Embedder()
    msg = EmbeddingMsg(
        message="fixture",
        context_data={
            "uri": "viking://resources/fixture",
            "account_id": "fixture",
            "id": "fixture",
        },
    )
    outcomes = []
    for _ in range(deliveries):
        result = await handler.on_dequeue({"data": msg.to_json()})
        outcomes.append(result.outcome.value)
        if result.outcome.value != "requeued":
            break
        msg = messages[-1]
    return {
        "scenario": "queue_" + classify_api_error(RuntimeError(kind)),
        "model_boundary_calls": calls,
        "deliveries_observed": len(outcomes),
        "outcomes": outcomes,
        "same_message_id": all(m.id == msg.id for m in messages),
    }


async def prototype_replay(status=429, code="TooManyRequests", online=False, credentials=1):
    transport = Transport(status, code)
    client = transport.client(max_retries=0)
    owner = ModelRetryOwner(sleep=AsyncMock(), jitter=lambda: 1.0)
    ctx = RetryContext(
        "add_resource",
        "embed_resource",
        "online" if online else "offline",
        "fixture-logical-call",
        time.time() + 60,
    )

    async def once(credential, timeout):
        try:
            return await client.embeddings.create(model="fixture", input="fixture", timeout=timeout)
        except Exception as exc:
            raise ModelFailure(classify_api_error(exc)) from exc

    try:
        # Reusing a serialized terminal context must not reset the attempt count.
        for _ in range(3):
            await expect_failure(
                lambda ctx=ctx: owner.execute(
                    ctx, once, credentials=credentials, allow_credential_failover=credentials > 1
                )
            )
            ctx = RetryContext.restore(json.loads(json.dumps(ctx.snapshot())))
        return {
            "scenario": f"prototype_{status}_online_{online}_credentials_{credentials}",
            "http_requests": transport.requests,
            "attempts_used": ctx.attempts_used,
            "terminal_reason": ctx.terminal,
            "logical_outcomes": sum(e["event"] == "logical_call" for e in owner.events),
        }
    finally:
        await client.close()


async def main():
    logging.disable(logging.CRITICAL)
    with (
        patch("asyncio.sleep", new=AsyncMock()),
        patch("openviking.utils.model_retry._compute_delay", return_value=0.0),
    ):
        baseline = [
            await embedding_baseline(),
            await vlm_baseline(401, "Unauthorized"),
            await vlm_baseline(outer=True),
            await vlm_baseline(outer=True, credentials=2),
        ]
        queues = [
            await queue_baseline(error)
            for error in [
                "429 TooManyRequests",
                "content_filter",
                "429 AccountQuotaExceeded",
                "401 Unauthorized",
                "unexpected validation error",
            ]
        ]
        proposed = [
            await prototype_replay(),
            await prototype_replay(online=True),
            await prototype_replay(credentials=2),
            await prototype_replay(401, "Unauthorized"),
        ]
    assert [r["http_requests"] for r in baseline] == [12, 4, 16, 32], baseline
    assert [r["http_requests"] for r in proposed] == [4, 1, 4, 1], proposed
    assert [r["outcomes"] for r in queues] == [
        ["requeued"] * 3,
        ["requeued"] * 3,
        ["requeued"] * 3,
        ["failed"],
        ["requeued"] * 3,
    ], queues
    report = {
        "git_baseline": BASELINE,
        "sdk_version": importlib.metadata.version("volcengine-python-sdk"),
        "sdk_default_max_retries": inspect.signature(AsyncArk).parameters["max_retries"].default,
        "transport": "httpx.MockTransport; no external HTTP requests",
        "baseline": baseline,
        "queue_handler": queues,
        "prototype": proposed,
        "limitations": [
            "Composed Phase-2 retry helper, not a full Session/add_resource E2E",
            "QueueFS process crash, durable reservation and live metrics exporter are not tested",
            "Synthetic failures have no usage or billing evidence",
        ],
    }
    return report


if __name__ == "__main__":
    from openviking.session import session as session_module

    if not hasattr(session_module, "_MEMORY_EXTRACTION_MAX_RETRIES"):
        raise SystemExit(
            "This is a baseline replay. Set PYTHONPATH to a clean 611f5c469b2 checkout; use tests/models/test_model_retry_transport.py for the migrated code."
        )
    baseline_root = Path(inspect.getfile(VolcengineDenseEmbedder)).resolve().parents[3]
    repository = Path(__file__).resolve().parents[2]
    for source in (
        "openviking/models/embedder/base.py",
        "openviking/models/embedder/volcengine_embedders.py",
        "openviking/models/vlm/base.py",
        "openviking/models/vlm/backends/volcengine_vlm.py",
        "openviking/session/session.py",
        "openviking/storage/collection_schemas.py",
        "openviking/utils/model_retry.py",
    ):
        expected = subprocess.check_output(["git", "show", f"{BASELINE}:{source}"], cwd=repository)
        if (baseline_root / source).read_bytes() != expected:
            raise SystemExit(f"Imported source differs from the pinned baseline: {source}")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="Write the JSON report separately from library logs"
    )
    args = parser.parse_args()
    result = json.dumps(asyncio.run(main()), indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
        print(f"Saved report to {args.output}")
    else:
        print(result)
