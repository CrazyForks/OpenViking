"""Shared record transforms, soft batching and job accounting for Compile operators.

Functions use the supplied Pipeline state directly; model I/O and repair policy belong
to pipeline_io. Record transforms are shared by Map and intermediate Reduce stages.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from openviking.core.namespace import relative_uri_path
from vikingbot.compile import file_ops
from vikingbot.compile.plan import (
    FileDraft,
    FileResponse,
    Group,
    InputReferenceError,
    MissingReadyPathError,
    Record,
    RecordResponse,
    digest,
)
from vikingbot.compile.renderer import validate_relative_file_path

if TYPE_CHECKING:
    from vikingbot.compile.pipeline import Pipeline


_FIDELITY = """Do not add plausible business consequences, instructions or definitions that the
sources do not establish. Do not turn examples into rules. Retain ambiguity in the actor of a
condition, conjunctions, slash notation and missing units; quote an unclear clause instead of
selecting a plausible interpretation or inventing an obligation.
"""

_RECORDS = (
    _FIDELITY
    + """Transform supplied inputs into the declared record fields, preserving the Skill.
Use record_fields names and descriptions as a guide for payload facts, which may contain structured JSON.
Use short scope keys with evidence-based values; scope_fields explains each suggested field.
Omit unavailable fields and add useful fields when the evidence calls for them.
If ready_content is non-null, ready_path MUST be a non-empty relative file
path under the compile target.
Put both fields at the record's top level, never inside payload.
Before calling emit, check this pairing for every record.
Each record.inputs lists ONLY supplied input IDs supporting its payload; runtime assigns IDs,
stores complete source evidence and propagates provenance. Return records only; runtime tracks
unreferenced inputs. References establish provenance, not semantic completeness.
Read all supplied text; preserve required detail, citations, exceptions and applicability conditions.
Use short routing_text; never group just by title. Independent finished files use ready_content with
ready_path and concise identity/scope/relationship payloads. Evidence details already in the body
need not be repeated in payload. Check finished content against originals and every Skill rule.
Fragments requiring joint synthesis retain full necessary evidence in payload and no ready content.
Other files generated in this assignment can be referenced by their exact titles; runtime resolves
unambiguous mentions after ownership and final paths are accepted. Never assume proposed paths exist.
Determine boundaries and relations before writing. Each page has one primary Skill classification
and every required directory level; shared sources/products do not unite different reader purposes.
For OKF Wiki, write content pages only; runtime owns navigation, so never draft or emit OKF index.md.
Ordinary non-Wiki files follow their Skill.
"""
)


async def payload(runtime: Pipeline, record: Record) -> dict:
    """Resolve one complete assignment payload; evidence files remain addressable."""
    data = await runtime.files.get(record.payload_ref)
    if data is None:
        raise ValueError(f"Missing evidence/payload: {record.payload_ref}")
    item = {"id": record.record_id, "payload": data}
    if record.ready_ref:
        item["ready_file"] = await runtime.files.get(record.ready_ref)
    return item


async def pack(
    runtime: Pipeline, records, system, schema, extra=None, max_payload_chars=None
) -> list[list[Record]]:
    """Split records around soft size targets without rejecting indivisible records.

    Large records remain complete in their own batches. Extraction also uses an
    output-size estimate to avoid packing too many expanding inputs together.
    """
    batches: list[list[Record]] = []
    current: list[Record] = []
    payloads: list[dict] = []
    for record in records:
        item = await payload(runtime, record)
        candidate = {"inputs": [*payloads, item], **(extra or {})}
        # Keep input ID arrays bounded even when source files are tiny.
        if current and (
            len(current) >= 32
            or not runtime.model.fits(system, candidate, schema)
            or (
                max_payload_chars is not None
                and len(json.dumps(candidate["inputs"], ensure_ascii=False)) > max_payload_chars
            )
        ):
            batches.append(current)
            current, payloads = [], []
        current.append(record)
        payloads.append(item)
    if current:
        batches.append(current)
    return batches


def validate_input_refs(inputs, included) -> None:
    """Reject references outside the supplied inputs without requiring complete coverage."""
    expected = {r.record_id for r in inputs}
    unknown = set(included) - expected
    if unknown:
        raise InputReferenceError(
            f"Input references must name supplied inputs: unknown={sorted(unknown)}"
        )


async def transform(runtime: Pipeline, label, transform, records, extra=None) -> list[Record]:
    """Produce typed records with deterministic IDs and runtime-propagated evidence sets."""
    system = runtime.system + _RECORDS + "\nTask: " + transform.instructions
    data = {
        "inputs": [await payload(runtime, r) for r in records],
        **(extra or {}),
        "record_fields": transform.fields,
        "scope_fields": runtime.contract.distinguish,
    }

    def validate(response):
        validate_input_refs(records, [i for draft in response.records for i in draft.inputs])
        for index, draft in enumerate(response.records):
            if draft.ready_content_ref is not None:
                raise ValueError("Ready references require an agent with scratch access")
            # Agent file references are resolved before checking record content.
            if not draft.payload and not (draft.ready_content or "").strip():
                raise ValueError("A record requires business payload or nonempty ready content")
            if draft.ready_path is not None:
                validate_relative_file_path(draft.ready_path)
            if draft.ready_content is not None and draft.ready_path is None:
                raise MissingReadyPathError(
                    f"records[{index}]: ready_content is present but ready_path is missing. "
                    "Add a top-level ready_path with an appropriate relative file path. "
                    "Preserve the existing ready_content; do not remove it to bypass validation."
                )
            if draft.ready_content is not None:
                ready_group = Group("map", records)
                file_ops.validate_files(
                    runtime,
                    FileResponse(
                        files=[
                            FileDraft(
                                path=draft.ready_path,
                                content=draft.ready_content,
                                inputs=draft.inputs,
                            )
                        ],
                    ),
                    ready_group,
                    [r for r in records if r.record_id in draft.inputs],
                    {},
                )
            if draft.target_uri and (
                not relative_uri_path(runtime.target, draft.target_uri)
                or draft.target_uri
                not in json.dumps(
                    [item for item in data["inputs"] if item["id"] in draft.inputs],
                    ensure_ascii=False,
                )
            ):
                raise ValueError(
                    "A stable target candidate must occur explicitly in supplied evidence inside to"
                )

    response = await runtime.model.ask(
        label, system, data, RecordResponse, validate, agent=transform.execution == "agent"
    )
    outputs = []
    by_id = {r.record_id: r for r in records}
    for index, draft in enumerate(response.records):
        source_refs = sorted({ref for parent in draft.inputs for ref in by_id[parent].source_refs})
        record_id = digest([label, data, index, draft.model_dump(), runtime.model.identity])[:24]
        payload_ref = f"payloads/{record_id}"
        # Only short source identifiers and runtime counts enter the next prompt.
        evidence_uris = sorted({runtime.evidence[ref]["uri"] for ref in source_refs})
        await runtime.files.put(
            payload_ref,
            {
                "fields": draft.payload,
                "evidence_ref": f"records/{record_id}",
                "unique_source_count": len(evidence_uris),
                "source_examples": evidence_uris[:8],
                "source_ranges": source_refs,
                "routing_text": draft.routing_text,
                "scope": draft.scope,
            },
        )
        record = Record(
            record_id,
            payload_ref,
            source_refs,
            draft.routing_text,
            draft.scope,
            draft.inputs,
            f"ready/{record_id}" if draft.ready_content is not None else None,
            digest(transform.model_dump()),
            draft.target_uri,
        )
        if record.ready_ref:
            await runtime.files.put(
                record.ready_ref, {"path": draft.ready_path, "content": draft.ready_content}
            )
        runtime.register(record)
        await runtime.files.put(f"records/{record_id}", asdict(record))
        outputs.append(record)
    for record_id in by_id.keys() - {i for draft in response.records for i in draft.inputs}:
        runtime.status[record_id] = "unreferenced"
    return outputs


async def job(runtime: Pipeline, name, inputs, work):
    """Persist one job's outcome; failures never advance source/record completion."""
    entry = {"status": "running", "inputs": [r.record_id for r in inputs]}
    await runtime.files.put(f"jobs/{name}", entry)
    try:
        try:
            output = await work()
        except OSError:
            runtime.metrics["job_retries"] += 1
            output = await work()
    except BaseException as exc:
        state = "failed" if isinstance(exc, Exception) else "pending"
        for record in inputs:
            runtime.status[record.record_id] = state
        await runtime.files.put(f"jobs/{name}", {**entry, "status": state, "error": str(exc)[:800]})
        raise
    await runtime.files.put(
        f"jobs/{name}", {**entry, "status": "completed", "output_count": len(output)}
    )
    return output
