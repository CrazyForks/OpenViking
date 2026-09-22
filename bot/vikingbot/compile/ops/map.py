"""Map independent source units or intermediate records into records or file candidates."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from vikingbot.compile.ops import common
from vikingbot.compile.ops import reduce as reduce_op
from vikingbot.compile.pipeline_io import bounded_jobs
from vikingbot.compile.plan import Group, Node, Record, RecordResponse

if TYPE_CHECKING:
    from vikingbot.compile.pipeline import Pipeline


async def run(runtime: Pipeline, node: Node, inputs: list[Record]) -> list[Record] | list[str]:
    """Batch inputs for the selected transform and retain successful job outputs.

    File-granularity source assignments retain all ranges of one URI in offset order.
    Other agent/file-output assignments contain one record; direct record calls batch.
    Job failures update runtime state while other assignments continue.
    """
    transform = getattr(runtime.contract, node.task)
    if node.source == "sources" and transform.input_unit == "file":
        files: dict[str, list[Record]] = {}
        for record in inputs:
            files.setdefault(runtime.evidence[record.record_id]["uri"], []).append(record)
        jobs = [
            sorted(parts, key=lambda r: runtime.evidence[r.record_id]["start_char"])
            for parts in files.values()
        ]
    elif transform.execution == "agent" or transform.output == "files" or node.source != "sources":
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
    result = [record for output in outputs for record in output]
    return await reduce_op.resolve_files(runtime, result) if transform.output == "files" else result


async def map_job(runtime: Pipeline, node, item):
    """Expand one packed Map assignment without a parent agent spawning children."""
    index, records = item
    transform = getattr(runtime.contract, node.task)
    name = f"{node.name}-{index}"
    return await common.job(
        runtime,
        name,
        records,
        lambda: (
            reduce_op.reduce_group(runtime, node, Group(name, records), stage="map")
            if transform.output == "files"
            else common.transform(runtime, "map", transform, records)
        ),
    )
