"""Reduce candidate work sets into records or revision-bound files.

Structured overflow aggregation and same-path candidate synthesis belong to Reduce;
Finalize prepares the accepted file collection for publication without model calls.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from openviking.core.namespace import relative_uri_path
from vikingbot.compile import file_ops
from vikingbot.compile.ops import common
from vikingbot.compile.ops.common import _FIDELITY, _RECORDS
from vikingbot.compile.pipeline_io import bounded_jobs
from vikingbot.compile.plan import (
    FileDraft,
    FileResponse,
    Group,
    Node,
    Record,
    RecordResponse,
    Transform,
    content_hash,
    digest,
)

if TYPE_CHECKING:
    from vikingbot.compile.pipeline import Pipeline


_FILES = (
    _FIDELITY
    + """Generate final files for this output group following all Skill requirements.
Submit files[].path with content or content_ref. ready_* keys belong only to record transforms.
Original evidence is authoritative; derived payload fields are fallible extraction notes.
Verify numbers, units, boundaries, exceptions and claimed uncertainty against original_evidence.
original_evidence may contain excerpts marked complete=false; absence from an excerpt is not absence
from the source. Expand with read_evidence when conditions, table context or referenced clauses are
missing, ambiguous or conflicting; omit line bounds to read the full shard. Correct extraction mistakes.
Use original source citations carried by evidence; preserve exceptions and applicability conditions.
For existing files, prefer exact unique-anchor patches and return the supplied base_hash.
Patch anchors must be copied from the supplied old text, occur exactly once and not overlap.
Full content replacement is allowed for small files or whole-file changes; do not mix with patches.
For a selected large-file section, patches can only modify that section; never replace other text.
For new files return exact content and no base_hash. Each work set contains candidate evidence,
not a predetermined file: combine supplements, preserve conflicting facts with their applicability,
and split independent subjects into as many files as needed. Input paths and scopes do not bind
output paths. historical_files supplies recalled paths, content and base_hash for comparison;
update relevant files using their exact path and hash, leaving irrelevant history unchanged.
A Skill's overall required outputs are fulfilled collectively across work sets.
related_outputs is a partial catalog of accepted current-task files with real final paths. Use
relevant entries for links and avoid duplicating their full bodies. An unlisted page is unknown,
not absent; never claim another group has not produced a page. When a target path is unknown,
mention the relevant subject naturally without inventing a destination.
related_subjects contains assigned topics, not accepted files; do not treat them as link targets.
Set each file.inputs to supplied input IDs actually used by that file. Multiple inputs may support
one file; output counts and independence follow the Skill and stage task. Do not claim unrelated sources.
Wiki files require YAML type, title, single-line description and source citations. Generic files
follow their declared format. Never fabricate requirements, source identities or existing paths.
"""
)


async def run(runtime: Pipeline, node: Node, groups: list[Group]) -> list[Record] | list[str]:
    """Transform groups concurrently; only resolved file candidates become publishable."""
    outputs = await bounded_jobs(
        groups,
        partial(reduce_job, runtime, node),
        concurrency=runtime.limits.merge_concurrency,
        metrics=runtime.metrics,
        failures=runtime.failures,
    )
    references = [item for output in outputs for item in output]
    if getattr(runtime.contract, node.task).output == "records":
        return references
    return await resolve_files(runtime, references)


async def resolve_files(runtime: Pipeline, references: list[str]) -> list[str]:
    """Accept unique paths and resolve collisions using the Skill and request.

    Concurrent decisions reserve renamed paths before saving; a competing reservation
    permits one replan with the current path set. Failed decisions stay unpublished; successful
    unrelated outputs remain available for partial recovery. Candidates stay on disk.
    """
    candidates = {}
    for reference in references:
        artifact = await runtime.files.get(reference)
        candidates.setdefault(artifact["path"], []).append((reference, artifact))
    reserved, result = {path: path for path in candidates}, []
    for items in candidates.values():
        if len(items) == 1:
            result.append(items[0][0])
    await file_ops.accept_files(runtime, result)

    async def resolve(items):
        """Resolve one collision, reserving every selected path before yielding to storage."""
        path = items[0][1]["path"]
        inputs = {i for _, artifact in items for i in artifact["inputs"]}
        records = [runtime.records[i] for i in sorted(inputs)]
        group = Group("merge-" + digest([ref for ref, _ in items]), records)

        def validate(response):
            file_ops.validate_files(runtime, response, group, records, {})
            blocked = {d.path for d in response.files if reserved.get(d.path, path) != path}
            if blocked:
                raise ValueError(f"Output paths are reserved by other jobs: {sorted(blocked)}")
            if {i for draft in response.files for i in draft.inputs} != inputs:
                raise ValueError(
                    "Resolved files must account for every candidate's supporting input"
                )

        try:
            transform = Transform(
                output="files",
                instructions=(
                    "Resolve candidate path collisions following the Skill and instruction. "
                    "Combine compatible contributions, deduplicate equivalents, or rename independent "
                    "files. Preserve required detail and input independence. Submit complete files "
                    "with supporting inputs, without patches or base_hash; runtime binds revisions."
                ),
            )
            for attempt in range(2):
                unavailable = sorted(p for p, owner in reserved.items() if owner != path)
                try:
                    response = await common.ask_transform(
                        runtime,
                        "reduce_merge",
                        transform,
                        runtime.system + "\n" + transform.instructions,
                        {
                            "candidates": [a for _, a in items],
                            "reserved_paths": unavailable,
                            "inputs": [await common.payload(runtime, r) for r in records],
                        },
                        FileResponse,
                        validate,
                    )
                    validate(response)
                except ValueError:
                    if attempt or unavailable == sorted(
                        p for p, owner in reserved.items() if owner != path
                    ):
                        raise
                else:
                    reserved.update((draft.path, path) for draft in response.files)
                    break
            resolved = await file_ops.save_replacements(
                runtime, response, group, records, origin=path
            )
            await file_ops.accept_files(runtime, resolved)
            return resolved
        except (OSError, ValueError) as exc:
            runtime.failures.append(f"Unresolved output path {path}: {exc}")
            for record in records:
                runtime.status[record.record_id] = "failed"
            return []

    resolved = await bounded_jobs(
        (items for items in candidates.values() if len(items) > 1),
        resolve,
        concurrency=runtime.limits.merge_concurrency,
        metrics=runtime.metrics,
    )
    return result + [reference for batch in resolved for reference in batch]


async def reduce_job(runtime: Pipeline, node, group: Group):
    """Synthesize a work set into records or any supported collection of files."""
    return await common.job(
        runtime,
        f"{node.name}-{group.group_id}",
        group.records,
        lambda: reduce_group(runtime, node, group),
    )


async def ready_file(runtime: Pipeline, group):
    """Reuse only a solitary complete draft with no historical comparison to perform."""
    if group.target_uris or len(group.records) != 1 or not group.records[0].ready_ref:
        return None
    return await runtime.files.get(group.records[0].ready_ref)


async def reduce_group(runtime: Pipeline, node, group: Group, *, stage="reduce"):
    """Transform a work set into records or file candidates; Map binds full replacements."""
    transform = getattr(runtime.contract, node.task)
    records, old = group.records, {}
    if transform.output == "files":
        for uri in group.target_uris:
            path = relative_uri_path(runtime.target, uri)
            if not path:
                raise ValueError("Historical context is outside the target")
            content = await file_ops.load_old(runtime, path)
            if content is None:
                raise ValueError(f"Recalled historical file no longer exists: {uri}")
            old[path] = content
    ready = (
        await ready_file(runtime, group)
        if stage == "reduce" and transform.output == "files"
        else None
    )
    if ready is not None:
        previous = await file_ops.load_old(runtime, ready["path"])
        if previous is not None:
            old[ready["path"]] = previous
        else:
            response = FileResponse(
                files=[
                    FileDraft(
                        path=ready["path"],
                        content=ready["content"],
                        inputs=[records[0].record_id],
                    )
                ],
            )
            file_ops.validate_files(runtime, response, group, records, old)
            runtime.metrics["direct_reuse"] += 1
            return await file_ops.save_files(runtime, response, group, records, old)
    evidence = sorted({ref for r in records for ref in r.source_refs})
    extra = {
        "group": {"id": group.group_id},
        "scope_fields": runtime.contract.distinguish,
        "unique_source_count": len({runtime.evidence[ref]["uri"] for ref in evidence}),
    }
    if transform.output == "files":
        related = [
            entry
            for path, entry in runtime.catalog.items()
            if set(entry["source_refs"]) & set(evidence)
        ]
        extra["related_outputs"] = [
            {k: v for k, v in entry.items() if k != "source_refs"}
            for entry in sorted(related, key=lambda e: e["path"])[:12]
        ]
        extra["related_subjects"] = [
            {"scope": r.scope, "description": r.routing_text}
            for r in runtime.records.values()
            if r.schema == records[0].schema
            and r not in records
            and set(r.source_refs) & set(evidence)
        ][:12]
    if old:
        extra["historical_files"] = [
            {"path": path, "content": content, "base_hash": content_hash(content)}
            for path, content in old.items()
        ]
    schema = RecordResponse if transform.output == "records" else FileResponse
    if transform.output == "records":
        extra.update(record_fields=transform.fields, scope_fields=runtime.contract.distinguish)
    system = (
        runtime.system
        + (_RECORDS if transform.output == "records" else _FILES)
        + "\nTask: "
        + transform.instructions
    )
    for depth in range(4):
        data = {**extra, "inputs": [await common.payload(runtime, r) for r in records]}
        if (
            runtime.model.fits(system, data, schema)
            or depth == 3
            or stage == "map"
            or runtime.contract.overflow != "structured"
        ):
            break
        combine = runtime.contract.combine
        assert combine is not None
        combine_system = runtime.system + _RECORDS + "\nTask: " + combine.instructions
        chunks = await common.pack(
            runtime,
            records,
            combine_system,
            RecordResponse,
            {"record_fields": combine.fields, "scope_fields": runtime.contract.distinguish},
        )
        reduced = []
        for chunk in chunks:
            reduced.extend(await common.transform(runtime, "combine", combine, chunk))
        if not reduced:
            raise ValueError("Overflow aggregation cannot discard all required contributions")
        records = reduced
    if transform.output == "records":
        return await common.transform(runtime, "reduce", transform, records, extra)
    response = await common.ask_transform(
        runtime,
        stage,
        transform,
        system,
        data,
        FileResponse,
        lambda value: file_ops.validate_files(runtime, value, group, records, old),
    )
    if stage == "map":
        return await file_ops.save_replacements(runtime, response, group, records)
    return await file_ops.save_files(runtime, response, group, records, old)
