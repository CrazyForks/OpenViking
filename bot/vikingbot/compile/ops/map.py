"""Map source ranges or intermediate records into source-linked extraction records."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vikingbot.compile.ops import common
from vikingbot.compile.pipeline_io import bounded_jobs
from vikingbot.compile.plan import Node, Record, RecordResponse

if TYPE_CHECKING:
    from vikingbot.compile.pipeline import Pipeline


async def run(runtime: Pipeline, node: Node, inputs: list[Record]) -> list[Record]:
    """Batch inputs for the selected transform and retain successful job outputs.

    Agent assignments contain one record; direct calls use soft size targets.
    Job failures update runtime state while other assignments continue.
    """
    transform = getattr(runtime.contract, node.task)
    if transform.execution == "agent":
        jobs = [[record] for record in inputs]
    else:
        jobs = await common.pack(
            runtime,
            inputs,
            runtime.system + common._RECORDS + "\nTask: " + transform.instructions,
            RecordResponse,
            {
                "record_fields": transform.fields,
                "scope_fields": runtime.contract.distinguish,
            },
            max_payload_chars=runtime.model.reserve,
        )
    outputs = await bounded_jobs(
        enumerate(jobs),
        partial(map_job, runtime, node),
        concurrency=runtime.limits.source_concurrency,
        metrics=runtime.metrics,
        failures=runtime.failures,
    )
    return [record for output in outputs for record in output]


async def map_job(runtime: Pipeline, node, item):
    """Expand one packed Map assignment without a parent agent spawning children."""
    index, records = item
    return await common.job(
        runtime,
        f"{node.name}-{index}",
        records,
        lambda: common.transform(runtime, "map", getattr(runtime.contract, node.task), records),
    )
