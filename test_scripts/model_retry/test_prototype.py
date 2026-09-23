"""Behavioral acceptance cases for the design sketch; no network or model usage."""

import asyncio
import json
import unittest

from prototype import (
    ModelCallStopped,
    ModelFailure,
    ModelRetryOwner,
    OperationRetryQuota,
    RetryContext,
)


class Clock:
    now = 0.0

    async def sleep(self, delay):
        self.now += delay


class RetryContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.owner = ModelRetryOwner(
            clock=lambda: self.clock.now, sleep=self.clock.sleep, jitter=lambda: 1.0
        )
        self.requests = []

    def context(self, **changes):
        args = {
            "operation": "add_resource",
            "stage": "embed_resource",
            "workload": "offline",
            "logical_call_id": "call-1",
            "deadline_at": 60.0,
        }
        args.update(changes)
        return RetryContext(**args)

    def failed_request(self, kind, retry_after=0):
        async def once(credential, timeout):
            self.requests.append((credential, timeout))
            raise ModelFailure(kind, retry_after)

        return once

    async def test_persistent_transient_has_four_requests_total(self):
        with self.assertRaises(ModelCallStopped) as error:
            await self.owner.execute(self.context(), self.failed_request("transient"))
        self.assertEqual(error.exception.reason, "attempts")
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(sum(e["event"] == "logical_call" for e in self.owner.events), 1)

    async def test_online_never_retries_or_switches_credentials(self):
        for kind in ["transient", "auth", "quota_exceeded"]:
            self.requests.clear()
            with self.assertRaises(ModelCallStopped):
                await self.owner.execute(
                    self.context(workload="online"),
                    self.failed_request(kind),
                    credentials=3,
                    allow_credential_failover=True,
                )
            self.assertEqual(len(self.requests), 1)

    async def test_nontransient_errors_are_terminal(self):
        for kind in [
            "auth",
            "permanent",
            "content_safety",
            "quota_exceeded",
            "input_too_large",
            "unknown",
        ]:
            with self.subTest(kind=kind):
                self.requests.clear()
                with self.assertRaises(ModelCallStopped) as error:
                    await self.owner.execute(self.context(), self.failed_request(kind))
                self.assertEqual(error.exception.reason, kind)
                self.assertEqual(len(self.requests), 1)

    async def test_failover_consumes_same_budget(self):
        with self.assertRaises(ModelCallStopped):
            await self.owner.execute(
                self.context(),
                self.failed_request("transient"),
                credentials=3,
                allow_credential_failover=True,
            )
        self.assertEqual([c for c, _ in self.requests], [0, 1, 2, 2])

    async def test_auth_failover_does_not_repeat_failed_credential(self):
        with self.assertRaises(ModelCallStopped):
            await self.owner.execute(
                self.context(),
                self.failed_request("auth"),
                credentials=2,
                allow_credential_failover=True,
            )
        self.assertEqual([c for c, _ in self.requests], [0, 1])

    async def test_transient_then_success_preserves_availability(self):
        async def once(credential, timeout):
            self.requests.append(credential)
            if len(self.requests) < 3:
                raise ModelFailure("transient")
            return "ok"

        self.assertEqual(await self.owner.execute(self.context(), once), "ok")
        self.assertEqual(len(self.requests), 3)

    async def test_backoff_and_retry_after_obey_absolute_deadline(self):
        ctx = self.context(deadline_at=5)
        with self.assertRaises(ModelCallStopped) as error:
            await self.owner.execute(ctx, self.failed_request("transient", retry_after=10))
        self.assertEqual(error.exception.reason, "deadline")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][1], 5)

    async def test_expired_queued_work_makes_no_request(self):
        with self.assertRaises(ModelCallStopped):
            await self.owner.execute(self.context(deadline_at=0), self.failed_request("transient"))
        self.assertEqual(self.requests, [])

    async def test_serialized_exhaustion_cannot_gain_a_fresh_budget(self):
        ctx = self.context()
        for _ in range(5):
            with self.assertRaises(ModelCallStopped):
                await self.owner.execute(ctx, self.failed_request("transient"))
            ctx = RetryContext.restore(json.loads(json.dumps(ctx.snapshot())))
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(sum(e["event"] == "retry_exhausted" for e in self.owner.events), 1)

    async def test_handoff_inherits_consumed_attempts(self):
        ctx = RetryContext.restore(self.context(attempts_used=3).snapshot())
        with self.assertRaises(ModelCallStopped):
            await self.owner.execute(ctx, self.failed_request("transient"))
        self.assertEqual(len(self.requests), 1)

    async def test_normal_fanout_does_not_consume_retry_quota(self):
        quota = OperationRetryQuota(0)

        async def once(credential, timeout):
            self.requests.append(credential)
            return "ok"

        results = await asyncio.gather(
            *[
                self.owner.execute(
                    self.context(logical_call_id=f"call-{n}"), once, operation_quota=quota
                )
                for n in range(20)
            ]
        )
        self.assertEqual(results, ["ok"] * 20)
        self.assertEqual(len(self.requests), 20)

    async def test_siblings_share_extra_request_quota(self):
        quota = OperationRetryQuota(2)
        results = await asyncio.gather(
            *[
                self.owner.execute(
                    self.context(logical_call_id=f"call-{n}"),
                    self.failed_request("transient"),
                    operation_quota=quota,
                )
                for n in range(3)
            ],
            return_exceptions=True,
        )
        self.assertTrue(all(isinstance(r, ModelCallStopped) for r in results))
        self.assertEqual(len(self.requests), 5)  # Three initial + two retries.
        self.assertEqual(quota.remaining, 0)

    async def test_cancellation_does_not_generate_another_request(self):
        async def cancelled(credential, timeout):
            self.requests.append(credential)
            raise asyncio.CancelledError

        ctx = self.context()
        with self.assertRaises(asyncio.CancelledError):
            await self.owner.execute(ctx, cancelled)
        self.assertEqual(ctx.terminal, "cancelled")
        self.assertEqual(len(self.requests), 1)

    async def test_request_timeout_is_enforced(self):
        owner = ModelRetryOwner(request_timeout=0.01)

        async def hangs(credential, timeout):
            self.requests.append(credential)
            await asyncio.Event().wait()

        import time

        with self.assertRaises(ModelCallStopped):
            await owner.execute(self.context(workload="online", deadline_at=time.time() + 1), hangs)
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
