"""Executable design sketch; deliberately not imported by production code.

The adapter supplied to execute() must perform exactly one transport request and
normalize its failure. Queue/workflow code must settle ModelCallStopped, not start
a new context. Snapshots demonstrate cooperative handoff, NOT crash-safe storage.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class ModelFailure(Exception):
    """Normalized adapter failure, with no prompt or credential in its message."""

    def __init__(self, kind: str, retry_after: float = 0.0):
        super().__init__(kind)
        self.kind = kind
        self.retry_after = max(retry_after, 0.0)


class ModelCallStopped(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class RetryContext:
    operation: str
    stage: str
    workload: str
    logical_call_id: str
    deadline_at: float
    max_attempts: int = 4  # Total across all credentials, including first request.
    attempts_used: int = 0
    terminal: str = ""

    def __post_init__(self):
        if self.workload not in {"online", "offline"}:
            raise ValueError("workload must be online or offline")
        if self.max_attempts < 1 or self.attempts_used < 0:
            raise ValueError("invalid attempt budget")
        if not math.isfinite(self.deadline_at):
            raise ValueError("deadline must be finite")

    def snapshot(self) -> dict:
        return asdict(self)

    @classmethod
    def restore(cls, value: dict) -> RetryContext:
        return cls(**value)


@dataclass
class OperationRetryQuota:
    """Optional in-process quota for EXTRA requests, not ordinary fan-out.

    This object is shared by sibling coroutines on one event loop. It is not a
    distributed counter and must not be copied into separate queue messages.
    """

    remaining: int


class ModelRetryOwner:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.8, 1.2),
        request_timeout: float = 30.0,
    ):
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self.request_timeout = request_timeout
        self.events: list[dict] = []  # Test event sink; no Prometheus installation.
        self._running: set[int] = set()

    def emit(self, ctx: RetryContext, event: str, **fields):
        self.events.append(
            {"event": event, "operation": ctx.operation, "stage": ctx.stage, **fields}
        )

    def stop(self, ctx: RetryContext, reason: str):
        ctx.terminal = reason
        self.emit(ctx, "retry_decision", decision="stop", reason=reason, owner="model")
        if reason in {"attempts", "deadline", "operation_retry_quota"}:
            self.emit(ctx, "retry_exhausted", reason=reason)
        self.emit(ctx, "logical_call", result="failed")
        raise ModelCallStopped(reason)

    async def execute(
        self,
        ctx: RetryContext,
        execute_once: Callable[[int, float], Awaitable[T]],
        *,
        credentials: int = 1,
        allow_credential_failover: bool = False,
        operation_quota: OperationRetryQuota | None = None,
    ) -> T:
        """Only this layer retries. The callback receives route index and timeout.

        Route order here is illustrative: try the next configured credential on
        a transient failure; then retry the last. Auth/quota can only move to a
        different configured credential when explicitly enabled, never repeat it.
        """
        if credentials < 1:
            raise ValueError("at least one credential is required")
        if ctx.terminal:
            raise ModelCallStopped(ctx.terminal)
        if id(ctx) in self._running:
            raise ValueError("one logical call cannot have concurrent retry owners")
        self._running.add(id(ctx))
        limit = 1 if ctx.workload == "online" else ctx.max_attempts
        credential = 0
        try:
            while True:
                remaining = ctx.deadline_at - self.clock()
                if remaining <= 0:
                    self.stop(ctx, "deadline")
                if ctx.attempts_used >= limit:
                    self.stop(ctx, "attempts")
                is_extra = ctx.attempts_used > 0
                if is_extra and operation_quota is not None:
                    if operation_quota.remaining <= 0:
                        self.stop(ctx, "operation_retry_quota")
                    operation_quota.remaining -= 1
                # Reserve before I/O. This reservation is in memory, not durable.
                ctx.attempts_used += 1
                timeout = min(remaining, self.request_timeout)
                try:
                    result = await asyncio.wait_for(execute_once(credential, timeout), timeout)
                except asyncio.CancelledError:
                    self.emit(ctx, "attempt", result="cancelled", error_class="cancelled")
                    raise
                except Exception as error:
                    failure = (
                        error
                        if isinstance(error, ModelFailure)
                        else ModelFailure(
                            "transient" if isinstance(error, TimeoutError) else "unknown"
                        )
                    )
                    self.emit(ctx, "attempt", result="failed", error_class=failure.kind)
                    has_backup = allow_credential_failover and credential + 1 < credentials
                    retryable = failure.kind == "transient"
                    switch_only = failure.kind in {"auth", "quota_exceeded"} and has_backup
                    if ctx.workload == "online":
                        self.stop(ctx, "online_no_retry")
                    if not retryable and not switch_only:
                        self.stop(ctx, failure.kind)
                    if ctx.attempts_used >= limit:
                        self.stop(ctx, "attempts")
                    delay = (
                        0.0
                        if switch_only
                        else max(
                            failure.retry_after,
                            min(0.5 * 2 ** min(ctx.attempts_used - 1, 10), 8.0) * self.jitter(),
                        )
                    )
                    if self.clock() + delay >= ctx.deadline_at:
                        self.stop(ctx, "deadline")
                    if has_backup:
                        credential += 1
                    self.emit(
                        ctx,
                        "retry_decision",
                        decision="failover" if has_backup else "retry",
                        reason=failure.kind,
                        owner="model",
                    )
                    await self.sleep(delay)
                else:
                    self.emit(ctx, "attempt", result="success", error_class="none")
                    ctx.terminal = "success"
                    self.emit(ctx, "logical_call", result="success")
                    return result
        except asyncio.CancelledError:
            ctx.terminal = "cancelled"
            self.emit(ctx, "logical_call", result="cancelled")
            raise
        finally:
            self._running.remove(id(ctx))
