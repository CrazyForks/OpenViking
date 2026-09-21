"""Local checkpoint recovery after all default-pipeline Reduce groups finish."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from functools import partial
from pathlib import Path

from loguru import logger

from vikingbot.compile import file_ops
from vikingbot.compile.ops import merge as merge_op
from vikingbot.compile.ops import reduce as reduce_op
from vikingbot.compile.pipeline_io import ROOT, bounded_jobs
from vikingbot.compile.plan import Contract, Record, content_hash


def read_checkpoint(workspace: Path) -> dict:
    """Validate local shards without writes or model calls; reject incomplete Reduce.

    Recovery supports the four-stage resource pipeline before publication. Returned
    candidates exclude merged artifacts; completed merges must retain all inputs.
    """
    root = workspace / ROOT

    def read(name):
        return json.loads((root / f"{name}.json").read_text())

    def shards(name):
        return {p.stem: json.loads(p.read_text()) for p in sorted((root / name).glob("*.json"))}

    plan = read("plan")
    if [n["op"] for n in plan["nodes"]] != ["map", "shuffle", "reduce", "merge"]:
        raise ValueError("Recovery requires the four-stage pipeline")
    if (root / "merge.json").exists():
        raise ValueError("Recovery requires an unpublished candidate-merge checkpoint")
    groups = shards("groups")
    if not groups:
        raise ValueError("No completed Reduce groups")
    for key in groups:
        if read(f"jobs/{plan['nodes'][2]['name']}-{key}")["status"] != "completed":
            raise ValueError(f"Reduce group is incomplete: {key}")
    sources, records = shards("sources"), shards("records")
    candidates, completed = defaultdict(list), {}
    for key, artifact in shards("artifacts").items():
        if content_hash(artifact["content"]) != artifact["sha256"]:
            raise ValueError(f"Artifact hash mismatch: {key}")
        file_ops.validate_relative_file_path(artifact["path"])
        if not artifact["inputs"] or not set(artifact["inputs"]) <= records.keys():
            raise ValueError(f"Artifact inputs are unavailable: {key}")
        expected = {ref for i in artifact["inputs"] for ref in records[i]["source_refs"]}
        if set(artifact["source_refs"]) != expected or not expected <= sources.keys():
            raise ValueError(f"Artifact evidence is unavailable: {key}")
        reference = f"artifacts/{key}"
        if artifact["owner"].startswith("merge-"):
            if artifact["path"] in completed:
                raise ValueError("Ambiguous completed merge")
            completed[artifact["path"]] = (reference, artifact)
        else:
            group = groups.get(artifact["owner"])
            if group is None or not set(artifact["inputs"]) <= set(group["records"]):
                raise ValueError(f"Artifact does not belong to a completed group: {key}")
            candidates[artifact["path"]].append((reference, artifact))
    if not candidates:
        raise ValueError("No Reduce candidates")
    for path, (_, artifact) in completed.items():
        expected = {i for _, a in candidates[path] for i in a["inputs"]}
        if set(artifact["inputs"]) != expected:
            raise ValueError(f"Completed merge has incomplete lineage: {path}")
    return dict(
        contract=read("contract"), runtime=read("runtime"), sources=sources,
        records=records, candidates=dict(candidates), completed=completed,
        summary=read("summary") if (root / "summary.json").exists() else None,
    )


async def run(pipeline):
    """Resume only candidate synthesis and final Merge in a copied local workspace.

    The caller owns task status and publication. Source errors survive recovery;
    cancellation writes a resumable summary and never triggers publication here.
    """
    checkpoint = await asyncio.to_thread(read_checkpoint, pipeline.files.sandbox.workspace)
    summary = checkpoint["summary"]
    if summary is None or summary.get("committed"):
        raise ValueError("A stopped, unpublished task summary is required")
    contract, runtime = checkpoint["contract"], checkpoint["runtime"]
    if pipeline.skill_target:
        raise ValueError("Local recovery supports resource outputs only")
    if pipeline.skill != contract["skill"] or pipeline.request.skill != runtime["skill"]:
        raise ValueError("Skill differs from the checkpoint")
    if pipeline.model.identity != runtime["model_settings"]:
        raise ValueError("Model settings differ from the checkpoint")
    if not await pipeline.resources.valid(contract["dependencies"]):
        raise ValueError("Skill dependencies differ from the checkpoint")
    pipeline.contract = Contract.model_validate(contract["contract"])
    pipeline.system = (
        "Original Skill (authoritative):\n" + pipeline.skill
        + "\nInstruction:\n" + pipeline.request.instruction
        + "\nShared requirements:\n"
        + pipeline.contract.model_dump_json(include={"preserve", "validation", "required_paths"})
        + "\nRuntime: " + json.dumps(runtime)
        + "\nSkill attachments (read necessary rules before use): "
        + json.dumps(pipeline.resources.hashes)
    )
    for key, source in checkpoint["sources"].items():
        pipeline.evidence[key] = {k: v for k, v in source.items() if k not in {"text", "context"}}
        pipeline.register(Record(key, f"sources/{key}", [key], source["uri"], {}, []))
    for record in checkpoint["records"].values():
        pipeline.register(Record(**record))
    if not pipeline.records.keys() <= summary["states"].keys():
        raise ValueError("Checkpoint has incomplete record states")
    pipeline.status.update(summary["states"])
    pipeline.failures.extend(e for e in summary["errors"] if e != "CancelledError")
    pipeline.warnings.extend(summary["warnings"])
    pipeline.metrics.update(summary["metrics"])
    for field, suffix in [("prompt_tokens", "_prompt_tokens"),
                          ("completion_tokens", "_completion_tokens"),
                          ("cache_read_input_tokens", "_cache_read_input_tokens")]:
        pipeline.usage[field] = sum(v for k, v in pipeline.metrics.items() if k.endswith(suffix))
    pipeline.usage["total_tokens"] = pipeline.usage["prompt_tokens"] + pipeline.usage["completion_tokens"]
    candidates, completed = checkpoint["candidates"], checkpoint["completed"]
    for items in candidates.values():
        for _, artifact in items:
            if artifact["base_hash"]:
                old = await file_ops.load_old(pipeline, artifact["path"])
                if old is None or content_hash(old) != artifact["base_hash"]:
                    raise ValueError(f"Historical revision changed: {artifact['path']}")
    total = sum(len(items) > 1 for items in candidates.values())
    done, active = len(completed), 0
    logger.info("RESUME ready: merged={}/{} concurrency={}", done, total, pipeline.limits.merge_concurrency)

    async def consolidate(items):
        nonlocal done, active
        path = items[0][1]["path"]
        if path in completed:
            ref = completed[path][0]
            await file_ops.accept_files(pipeline, [ref])
            return ref
        merging = len(items) > 1
        active += int(merging)
        if merging:
            logger.info("RESUME merge start: active={} path={}", active, path)
        try:
            ref = await reduce_op.merge_candidates(pipeline, items)
            if merging:
                done += 1
                logger.info("RESUME merge finished: {}/{}", done, total)
            return ref
        finally:
            active -= int(merging)

    try:
        refs = await bounded_jobs(candidates.values(), consolidate,
                                  concurrency=pipeline.limits.merge_concurrency,
                                  metrics=pipeline.metrics)
        if pipeline.failures:
            pipeline.warnings.append("Partial output; source-stage failures retained during recovery.")
        return await merge_op.run(pipeline, refs, partial=bool(pipeline.failures))
    finally:
        await pipeline.write_coverage()
        await pipeline.files.put("summary", {
            **summary, "prepared": False, "committed": False, "states": pipeline.status,
            "metrics": dict(pipeline.metrics), "errors": pipeline.failures,
            "warnings": pipeline.warnings,
        })
