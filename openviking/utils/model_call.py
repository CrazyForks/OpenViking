# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""The sole retry owner for non-streaming model requests.

SDKs and callbacks must execute once. Credential wrappers explicitly delegate
one attempt to their selected backend. Independent nested calls retain their
own budgets. Workflows must not replay a terminal model outcome.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from functools import wraps
from typing import Awaitable, Callable, Sequence, TypeVar
from uuid import uuid4

from openviking.utils.model_retry import classify_api_error

T = TypeVar("T")
_logger = logging.getLogger(__name__)

OPERATIONS = frozenset({"add_resource", "session_commit", "find", "search", "other"})
STAGES = frozenset(
    {
        "parse",
        "file_summary",
        "directory_overview",
        "semantic_execute",
        "embed_resource",
        "embed_query",
        "archive_summary",
        "memory_extract",
        "skill_extract",
        "working_memory",
        "other",
    }
)
_OPERATION_ALIASES = {
    "resources.add_resource": "add_resource",
    "resources.add_skill": "add_resource",
    "add_resource_job": "add_resource",
    "session_commit_phase2": "session_commit",
    "session.commit": "session_commit",
    "retrieval.find": "find",
    "retrieval.search": "search",
    "search.find": "find",
    "search.search": "search",
}


@dataclass(frozen=True)
class ModelWorkload:
    operation: str = "other"
    workload: str = "online"
    stage: str = "other"
    deadline_at: float | None = None


_workload: ContextVar[ModelWorkload | None] = ContextVar("model_workload", default=None)
_model_stage: ContextVar[str | None] = ContextVar("model_call_stage", default=None)


@contextmanager
def model_stage(stage: str):
    """Attribute model calls without changing existing operation/token stages.

    An explicit model stage takes precedence over legacy telemetry stages. The
    latter remain the fallback for callers such as session commit that already
    provide suitable stage labels. Policy, deadline, and delegation are untouched.
    """
    token = _model_stage.set(stage if stage in STAGES else "other")
    try:
        yield
    finally:
        _model_stage.reset(token)


def _execution_identity() -> tuple[int, object]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@dataclass
class _Delegation:
    adapter: object
    execution: tuple[int, object]
    consumed: bool = False


_delegation: ContextVar[_Delegation | None] = ContextVar("model_delegation", default=None)


@contextmanager
def delegate_model_call(adapter: object):
    """Let a credential wrapper invoke exactly one attempt of this backend.

    This permission is tied to the selected adapter and execution task. A child
    task or an independent nested model call must obtain its own retry budget.
    """
    token = _delegation.set(_Delegation(adapter, _execution_identity()))
    try:
        yield
    finally:
        _delegation.reset(token)


def _take_delegation(adapter: object | None) -> bool:
    delegation = _delegation.get()
    if (
        adapter is None
        or delegation is None
        or delegation.adapter is not adapter
        or delegation.execution != _execution_identity()
        or delegation.consumed
    ):
        return False
    delegation.consumed = True
    return True


def current_model_workload() -> ModelWorkload:
    """Return the effective policy, attribution, and deadline for this context."""
    from openviking.telemetry.context import get_current_telemetry, get_current_telemetry_stage

    bound = _workload.get()
    operation = bound.operation if bound else get_current_telemetry().operation
    operation = _OPERATION_ALIASES.get(operation, operation)
    stage = _model_stage.get()
    if stage is None:
        stage = get_current_telemetry_stage() or (bound.stage if bound else "other")
    return ModelWorkload(
        operation=operation if operation in OPERATIONS else "other",
        workload=bound.workload if bound else "online",
        stage=stage if stage in STAGES else "other",
        deadline_at=bound.deadline_at if bound else None,
    )


@contextmanager
def model_workload(
    operation: str,
    *,
    workload: str = "offline",
    stage: str = "other",
    deadline_at: float | None = None,
):
    """Bind policy to this execution context, never to a shared model instance.

    Unbound calls are online (one attempt). An optional absolute deadline is
    enforced around async calls and before/after sync calls. Sync I/O still
    relies on the adapter's transport timeout to interrupt a blocking request;
    results arriving after the deadline are rejected. This is not a durable budget.
    """
    if workload not in {"online", "offline"}:
        raise ValueError("workload must be online or offline")
    if deadline_at is not None and not math.isfinite(deadline_at):
        raise ValueError("deadline_at must be finite")
    token = _workload.set(ModelWorkload(operation, workload, stage, deadline_at))
    try:
        yield
    finally:
        _workload.reset(token)


class ModelCallError(RuntimeError):
    """Terminal model outcome; queue/workflow retries must not grant a new budget."""

    model_retry_terminal = True

    def __init__(self, reason: str, error_class: str, attempts: int, logical_call_id: str):
        self.reason = reason
        self.error_class = error_class
        self.attempts = attempts
        self.logical_call_id = logical_call_id
        super().__init__(f"Model call stopped: {reason} ({error_class}, attempts={attempts})")


def get_model_call_error(error: BaseException) -> ModelCallError | None:
    """Read a terminal outcome without replacing a provider's exception type."""
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, ModelCallError):
            return error
        terminal = getattr(error, "model_call_error", None)
        if isinstance(terminal, ModelCallError):
            return terminal
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return None


def is_model_call_error(error: BaseException) -> bool:
    """Return whether the exception chain carries a terminal model outcome."""
    return get_model_call_error(error) is not None


def _retry_after(error: Exception) -> float:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        headers = getattr(getattr(error, "response", None), "headers", {}) or {}
        value = headers.get("retry-after")
        if value:
            try:
                delay = float(value)
            except (ValueError, TypeError):
                try:
                    delay = parsedate_to_datetime(value).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = 0.0
            if math.isfinite(delay):
                return max(0.0, delay)
        error = error.__cause__ or error.__context__
    return 0.0


class _Call:
    def __init__(self, model_type: str, max_retries: int, candidates: int):
        self.scope = current_model_workload()
        self.labels = {
            "model_type": model_type,
            "operation": self.scope.operation,
            "stage": self.scope.stage,
        }
        self.logical_call_id = uuid4().hex
        self.limit = max(0, int(max_retries)) + 1 if self.scope.workload == "offline" else 1
        self.attempts = 0
        self.route = 0
        self.candidates = candidates
        self.disabled: set[int] = set()
        self.finished = False

    def emit(self, event: str, **fields) -> None:
        from openviking.metrics.datasources.model_retry import ModelRetryEventDataSource

        ModelRetryEventDataSource.record(event, **self.labels, **fields)

    def finish(self, result: str) -> None:
        if not self.finished:
            self.finished = True
            self.emit("logical_call", result=result)

    def stop(self, reason: str, error_class: str, cause: Exception | None = None):
        _logger.warning(
            "Model call stopped operation=%s stage=%s logical_call_id=%s attempts=%s reason=%s error_class=%s",
            self.scope.operation,
            self.scope.stage,
            self.logical_call_id,
            self.attempts,
            reason,
            error_class,
        )
        self.emit("decision", decision="stop", reason=reason, owner="model")
        if reason in {"max_attempts", "deadline", "backoff_limit"}:
            self.emit("exhausted", reason=reason)
        self.finish("error")
        terminal = ModelCallError(reason, error_class, self.attempts, self.logical_call_id)
        if cause is not None:
            # Preserve SDK exception types/status/body for callers and the HTTP
            # error mapper. The attached terminal outcome survives cause wrappers.
            try:
                cause.model_call_error = terminal
                cause.model_retry_terminal = True
            except (AttributeError, TypeError):
                raise terminal from cause
            raise cause
        raise terminal from cause

    def remaining(self) -> float | None:
        if self.scope.deadline_at is None:
            return None
        return self.scope.deadline_at - time.time()

    def before_attempt(self) -> None:
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            self.stop("deadline", "transient")
        self.attempts += 1

    def failed(self, error: Exception) -> float:
        kind = classify_api_error(error)
        self.emit("attempt", result="error", error_class=kind)
        if is_model_call_error(error) or kind not in {"transient", "auth", "quota_exceeded"}:
            self.stop(kind, kind, error)
        if self.scope.workload == "online":
            self.stop("online", kind, error)
        if kind in {"auth", "quota_exceeded"}:
            self.disabled.add(self.route)
            if len(self.disabled) == self.candidates:
                self.stop(kind, kind, error)
        if self.attempts >= self.limit:
            self.stop("max_attempts", kind, error)
        next_route = next(
            (self.route + offset) % self.candidates
            for offset in range(1, self.candidates + 1)
            if (self.route + offset) % self.candidates not in self.disabled
        )
        retry_after = _retry_after(error) if kind == "transient" else 0.0
        if retry_after > 30.0:
            # Never wait without bound or retry earlier than the provider permits.
            self.stop("backoff_limit", kind, error)
        delay = max(random.uniform(0, min(30.0, 2 ** min(self.attempts - 1, 5))), retry_after)
        # Credential rejection is not a congestion signal.
        if kind in {"auth", "quota_exceeded"}:
            delay = 0.0
        remaining = self.remaining()
        if remaining is not None and delay >= remaining:
            self.stop("deadline", kind, error)
        self.emit(
            "decision",
            decision="failover" if next_route != self.route else "retry",
            reason=kind,
            owner="model",
        )
        self.route = next_route
        return delay


def run_model_sync(
    func: Callable[[], T],
    *,
    model_type: str,
    max_retries: int = 3,
    alternatives: Sequence[Callable[[], T]] = (),
    adapter: object | None = None,
    logger=None,
    operation_name: str = "",
) -> T:
    """Run one logical sync call with a shared attempt and credential budget.

    ``logger`` and ``operation_name`` are retained for adapter compatibility;
    model events use the bound workload and the owner's logger.
    """
    if _take_delegation(adapter):
        return func()
    callbacks = [func, *alternatives]
    call = _Call(model_type, max_retries, len(callbacks))
    while True:
        call.before_attempt()
        try:
            result = callbacks[call.route]()
        except Exception as error:
            delay = call.failed(error)
        except BaseException:
            call.emit("attempt", result="cancelled", error_class="cancelled")
            call.finish("cancelled")
            raise
        else:
            remaining = call.remaining()
            if remaining is not None and remaining <= 0:
                call.emit("attempt", result="error", error_class="transient")
                call.stop("deadline", "transient")
            call.emit("attempt", result="ok", error_class="none")
            call.finish("ok")
            return result
        time.sleep(delay)


async def run_model_async(
    func: Callable[[], Awaitable[T]],
    *,
    model_type: str,
    max_retries: int = 3,
    alternatives: Sequence[Callable[[], Awaitable[T]]] = (),
    adapter: object | None = None,
    logger=None,
    operation_name: str = "",
) -> T:
    """Run one logical async call within its attempt budget and deadline.

    ``logger`` and ``operation_name`` are retained for adapter compatibility;
    model events use the bound workload and the owner's logger.
    """
    if _take_delegation(adapter):
        return await func()
    callbacks = [func, *alternatives]
    call = _Call(model_type, max_retries, len(callbacks))
    try:
        while True:
            call.before_attempt()
            try:
                remaining = call.remaining()
                if remaining is None:
                    result = await callbacks[call.route]()
                else:
                    result = await asyncio.wait_for(
                        callbacks[call.route](), timeout=max(0, remaining)
                    )
            except asyncio.CancelledError:
                call.emit("attempt", result="cancelled", error_class="cancelled")
                raise
            except Exception as error:
                delay = call.failed(error)
            else:
                call.emit("attempt", result="ok", error_class="none")
                call.finish("ok")
                return result
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        call.finish("cancelled")
        raise


def model_call(model_type: str):
    """Decorate an execute-once adapter method (not a workflow or stream)."""

    def decorate(method):
        if asyncio.iscoroutinefunction(method):

            @wraps(method)
            async def async_call(self, *args, **kwargs):
                return await run_model_async(
                    lambda: method(self, *args, **kwargs),
                    model_type=model_type,
                    max_retries=self.max_retries,
                    adapter=self,
                )

            return async_call

        @wraps(method)
        def sync_call(self, *args, **kwargs):
            return run_model_sync(
                lambda: method(self, *args, **kwargs),
                model_type=model_type,
                max_retries=self.max_retries,
                adapter=self,
            )

        return sync_call

    return decorate
