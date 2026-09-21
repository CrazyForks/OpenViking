"""Candidate work sets preserve input coverage and version-checked multi-file outputs."""

import asyncio
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.compile import file_ops
from vikingbot.compile.models import CompileFailure, CompileLimits
from vikingbot.compile.ops import common
from vikingbot.compile.ops import merge as merge_op
from vikingbot.compile.ops import reduce as reduce_op
from vikingbot.compile.ops.shuffle import Shuffle
from vikingbot.compile.pipeline import Pipeline
from vikingbot.compile.pipeline_io import JsonModel, ModelCallError
from vikingbot.compile.plan import (
    Contract,
    FileResponse,
    Group,
    Record,
    RecordResponse,
    RouteResponse,
    Transform,
    content_hash,
)
from vikingbot.compile.service import BotCompileService
from vikingbot.compile.sources import pack_source_batches
from vikingbot.providers.base import LLMResponse, ToolCallRequest

from openviking_cli.exceptions import NotFoundError


@pytest.fixture
def runtime(request, monkeypatch):
    """Use real operators with source-linked in-memory shards and replaceable external calls."""
    pipeline = Pipeline(
        client=AsyncMock(),
        sandbox=AsyncMock(),
        provider=SimpleNamespace(),
        model="test",
        temperature=0,
        limits=CompileLimits(source_concurrency=2, merge_concurrency=2),
        request=SimpleNamespace(to="viking://resources/out", skill="viking://agent/skills/test"),
        skill="Preserve evidence and applicability.",
        usage={},
    )
    pipeline.contract = Contract(
        extract=Transform(instructions="Extract"),
        reduce=Transform(instructions="Synthesize", output="files"),
        routing="Joint evidence",
    )
    records = [
        Record(
            str(i),
            f"payloads/{i}",
            [str(i)],
            f"Evidence {i}",
            {"path": f"input-{i}.md", "identity_scope": f"Scope wording {i}"},
            [],
        )
        for i in range(getattr(request, "param", 3))
    ]
    shards = {}
    for record in records:
        pipeline.register(record)
        pipeline.evidence[record.record_id] = {
            "uri": f"viking://resources/source-{record.record_id}"
        }
        shards[record.payload_ref] = {
            "source_ranges": record.source_refs,
            "text": record.routing_text,
        }
    pipeline.files = SimpleNamespace(
        get=AsyncMock(side_effect=shards.get),
        put=AsyncMock(side_effect=lambda key, value: shards.update({key: value})),
    )
    pipeline.client.compile_embeddings.side_effect = lambda texts, **kw: {
        "model": "test",
        "vectors": [[1.0, 0.0] for _ in texts],
    }
    pipeline.model = SimpleNamespace(ask=AsyncMock(), fits=lambda *args: True)
    monkeypatch.setattr(
        file_ops, "load_old", AsyncMock(side_effect=lambda r, path: r.old.get(path))
    )
    return pipeline, shards, records


@pytest.mark.parametrize(
    "execution,multilevel,failed_map",
    [
        ("direct", False, False),
        ("agent", False, False),
        ("direct", True, False),
        ("agent", False, True),
    ],
)
async def test_pipeline_dispatch_preserves_datasets_and_partial_outputs(
    runtime, execution, multilevel, failed_map
):
    """Execute real operators with deterministic model replies through accepted artifacts.

    Custom plans reuse record transforms after intermediate Reduce; a failed Map
    assignment leaves unrelated outputs available and coverage explicitly incomplete.
    """
    pipeline, shards, _ = runtime
    sources = [(f"viking://resources/source-{i}", f"Evidence {i}") for i in range(2)]
    pipeline.request.instruction = "Keep facts"
    pipeline.request.from_ = [uri for uri, _ in sources]
    shards["inputs"] = dict.fromkeys(pipeline.request.from_, "pending")
    pipeline.client.find.return_value = {"resources": []}
    contract = pipeline.contract.model_dump()
    contract["extract"]["execution"] = execution
    proposal = {"contract": contract}
    if multilevel:
        contract["reduce"]["output"] = "records"
        contract["synthesize"] = {"instructions": "Write", "output": "files"}
        proposal["plan"] = (
            "records = p.map(sources, task=contract.extract)\n"
            "groups = p.shuffle(records, by=contract.routing)\n"
            "facts = p.reduce(groups, task=contract.reduce)\n"
            "checked = p.map(facts, task=contract.extract)\n"
            "final = p.shuffle(checked, by=contract.routing, against=target)\n"
            "changes = p.reduce(final, task=contract.synthesize)\n"
            "p.merge(changes, into=target)"
        )
    calls = []

    async def ask(stage, system, data, schema, validate, **kwargs):
        calls.append((stage, data, kwargs))
        if stage == "plan":
            value = proposal
        elif stage == "route":
            value = {
                "decisions": [
                    {"record": item["record"], "related": item["candidates"], "history": []}
                    for item in data["records"]
                ]
            }
        elif schema is RecordResponse:
            if failed_map and data["inputs"][0]["payload"].get("text") == "Evidence 1":
                raise ValueError("Extraction failed")
            value = {
                "records": [
                    {
                        "inputs": [item["id"]],
                        "payload": {"text": "Evidence"},
                        "routing_text": "Joint evidence",
                    }
                    for item in data["inputs"]
                ]
            }
        else:
            value = {
                "files": [
                    {
                        "path": "result.txt",
                        "content": "Complete evidence",
                        "inputs": [item["id"] for item in data["inputs"]],
                    }
                ]
            }
        response = schema.model_validate(value)
        validate(response)
        return response

    pipeline.model = JsonModel(
        SimpleNamespace(), "test", 0, pipeline.files, pipeline.limits, {}, pipeline.metrics
    )
    pipeline.model.ask = ask

    async def batches():
        for batch in pack_source_batches(sources, pipeline.limits):
            yield batch

    bundle = await pipeline.run(batches())
    assert bundle.operations == [
        {
            "uri": pipeline.target + "/result.txt",
            "content": "Complete evidence",
            "mode": "create",
        }
    ]
    assert shards["summary"]["prepared"] is not failed_map
    assert shards["summary"]["committed"] is False
    assert bool(shards["summary"]["errors"]) is failed_map
    assert shards["coverage"]["counts"] == (
        {"prepared": 1, "failed": 1} if failed_map else {"prepared": 2}
    )
    expected_stages = ["plan"] + ["map"] * (2 if execution == "agent" else 1)
    expected_stages += ["route"] * (1 if failed_map else 2) + ["reduce"]
    if multilevel:
        expected_stages += ["map", "route", "route", "reduce"]
    assert [stage for stage, _, _ in calls] == expected_stages
    for stage, data, kwargs in calls:
        if stage == "map":
            assert kwargs["agent"] is (execution == "agent")
            assert len(data["inputs"]) == (1 if execution == "agent" else 2)
    nodes = shards["plan"]["nodes"]
    assert len(nodes) == (7 if multilevel else 4)
    assert all(f"{node['name']}_milliseconds" in pipeline.metrics for node in nodes)
    artifact = shards[pipeline.artifacts[0]]
    assert shards["merge"]["outputs"]["result.txt"] == {
        "sha256": content_hash("Complete evidence"),
        "source_refs": artifact["source_refs"],
    }
    assert len(artifact["source_refs"]) == (1 if failed_map else 2)


@pytest.mark.parametrize("structured", [False, True])
async def test_reduce_overflow_uses_shared_record_transform_without_losing_lineage(
    runtime, structured
):
    """Soft limits preserve complete groups unless the contract enables structured combining."""
    pipeline, shards, records = runtime
    pipeline.contract.overflow = "structured" if structured else "direct"
    pipeline.contract.combine = Transform(instructions="Combine facts") if structured else None
    pipeline.model = JsonModel(
        SimpleNamespace(), "test", 0, pipeline.files, pipeline.limits, {}, pipeline.metrics
    )
    pipeline.model.fits = lambda system, data, schema: len(data["inputs"]) == 1
    stages = []

    async def ask(stage, system, data, schema, validate, **kwargs):
        stages.append(stage)
        ids = [item["id"] for item in data["inputs"]]
        if schema is RecordResponse:
            value = {
                "records": [
                    {
                        "inputs": ids,
                        "payload": {"text": "Complete facts"},
                        "routing_text": "Facts",
                    }
                ]
            }
        else:
            assert len(ids) == len(records)
            value = {"files": [{"path": "result.txt", "content": "Facts", "inputs": ids}]}
        response = schema.model_validate(value)
        validate(response)
        return response

    pipeline.model.ask = ask
    refs = await reduce_op.run(
        pipeline, SimpleNamespace(name="changes", task="reduce"), [Group("joint", records)]
    )
    # Indivisible records stop combining at the existing depth bound.
    assert stages == (["combine"] * 9 if structured else []) + ["reduce"]
    assert shards[refs[0]]["source_refs"] == [record.record_id for record in records]
    assert len(shards[refs[0]]["inputs"]) == len(records)


@pytest.mark.parametrize(
    "attempts, error",
    [
        (("missing", "missing", "valid"), None),
        (("schema", "missing", "valid"), None),
        (("missing", "missing", "missing"), "ready_path is missing"),
        (("schema", "schema"), "routing_text"),
        (("missing", "invalid"), "Unsafe relative path rejected"),
        (("truncated",), "Output limit reached"),
        (("facts",), None),
    ],
)
async def test_map_bounds_missing_ready_path_repairs(runtime, attempts, error):
    """Exercise real Map validation and repair limits with deterministic model responses."""
    pipeline, shards, records = runtime
    model = JsonModel(
        SimpleNamespace(), "test", 0, pipeline.files, pipeline.limits, {}, pipeline.metrics
    )
    pipeline.model = model
    responses = []
    for index, kind in enumerate(attempts):
        draft = {
            "inputs": [records[0].record_id],
            "routing_text": "Identity",
            "ready_content": "Complete evidence",
        }
        if kind == "valid":
            draft["ready_path"] = "page.txt"
        elif kind == "invalid":
            draft["ready_path"] = ""
        elif kind == "schema":
            draft.pop("routing_text")
        elif kind == "facts":
            draft.pop("ready_content")
            draft["payload"] = {"text": "Evidence"}
        responses.append(
            LLMResponse(
                None,
                [ToolCallRequest(str(index), "emit", {"records": [draft]}, 0)],
                finish_reason="length" if kind == "truncated" else "tool_calls",
            )
        )
    model.call = AsyncMock(side_effect=responses)
    if error:
        with pytest.raises(ValueError, match=error):
            await common.transform(pipeline, "map", pipeline.contract.extract, records[:1])
    else:
        output = await common.transform(pipeline, "map", pipeline.contract.extract, records[:1])
        if attempts[-1] == "valid":
            assert shards[output[0].ready_ref] == {
                "path": "page.txt",
                "content": "Complete evidence",
            }
        else:
            assert output[0].ready_ref is None
    assert model.call.await_count == len(attempts)
    assert pipeline.metrics["repairs"] == len(attempts) - 1
    if "missing" in attempts:
        feedback = model.call.call_args.args[1][-1]["content"]
        assert "records[0]: ready_content is present but ready_path is missing" in feedback
        assert "do not remove it to bypass validation" in feedback


@pytest.mark.parametrize("runtime", [10], indirect=True)
@pytest.mark.parametrize("historical", [False, True])
async def test_ten_inputs_produce_three_files_with_complete_lineage(runtime, historical):
    pipeline, shards, records = runtime
    paths = [f"final/{i}.txt" for i in range(3)]
    if historical:
        pipeline.old.update({paths[0]: "old-0", paths[1]: "old-1", "context.txt": "Unchanged"})
    shuffler = Shuffle(pipeline)
    history = [pipeline.target + "/" + path for path in pipeline.old]
    shuffler.candidates = AsyncMock(return_value=[{"uri": uri} for uri in history])

    async def ask(stage, system, data, schema, validate, **kwargs):
        if stage == "route":
            assert len(data["records"]) == 1
            assert all(len(item["candidates"]) <= 5 for item in data["records"])
            assert {r["id"] for r in data["inputs"]} >= {r["record"] for r in data["records"]}
            response = RouteResponse(
                decisions=[
                    {
                        "record": item["record"],
                        "related": [] if historical else item["candidates"],
                        "history": history,
                    }
                    for item in data["records"]
                ]
            )
        elif stage == "reduce_merge":
            ids = {key for draft in [data["main"], *data["others"]] for key in draft["inputs"]}
            return schema(content=",".join(sorted(ids, key=int)))
        else:
            assert stage == "reduce" and "path" not in data["group"]
            assigned = {item["id"] for item in data["inputs"]}
            files = []
            for i, path in enumerate(paths):
                ids = [str(j) for j in range(i, 10, 3) if str(j) in assigned]
                if not ids:
                    continue
                files.append(
                    {
                        "path": path,
                        "content": ",".join(ids),
                        "inputs": ids,
                        "base_hash": content_hash(pipeline.old[path])
                        if path in pipeline.old
                        else None,
                    }
                )
            response = FileResponse(files=files)
            if historical:
                assert {f["path"] for f in data["historical_files"]} == set(pipeline.old)
                for draft in response.files:
                    if draft.path in pipeline.old:
                        draft.base_hash = "stale"
                        with pytest.raises(ValueError, match="revision"):
                            validate(response)
                        draft.base_hash = content_hash(pipeline.old[draft.path])
        validate(response)
        return response

    pipeline.model.ask = ask
    groups = await shuffler.run(
        SimpleNamespace(name="groups", task="routing", against_target=historical), records
    )
    # Shared history does not join records; selected direct links determine membership.
    assert len(groups) == (10 if historical else 5)
    assert [record for group in groups for record in group.records] == records
    assert all(group.target_uris == sorted(history) for group in groups)
    assert pipeline.metrics["similarity_records"] == 10
    refs = await reduce_op.run(pipeline, SimpleNamespace(name="files", task="reduce"), groups)
    bundle = await merge_op.run(pipeline, refs)
    assert len(bundle.operations) == 3 and len(bundle.updated) == (2 if historical else 0)
    assert {shards[ref]["path"]: shards[ref]["source_refs"] for ref in refs} == {
        path: [str(j) for j in range(i, 10, 3)] for i, path in enumerate(paths)
    }
    assert all(op.get("expected_sha256") for op in bundle.operations if op["mode"] == "replace")
    assert set(pipeline.status.values()) == {"prepared"} and not pipeline.failures
    if not historical:
        pipeline.old[paths[0]] = "Existing outside the supplied history"
        guarded = await merge_op.run(pipeline, refs)
        assert len(guarded.operations) == 3
        assert all(operation["mode"] == "create" for operation in guarded.operations)


@pytest.mark.parametrize("runtime", [8], indirect=True)
@pytest.mark.parametrize(
    "outcome", ["independent", "malformed", "partial", "linked_partial", "cancelled"]
)
async def test_routing_preserves_same_path_inputs_and_propagates_cancellation(runtime, outcome):
    pipeline, _, records = runtime
    for record in records:
        record.scope["path"] = "shared.md"

    async def ask(stage, system, data, schema, validate):
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome in {"partial", "linked_partial"} and data["records"][0]["record"] == "0":
            raise ValueError("Routing unavailable for this batch")
        response = RouteResponse(
            decisions=[
                {
                    "record": item["record"],
                    "related": ["not-supplied"]
                    if outcome == "malformed"
                    else ["0"]
                    if outcome == "linked_partial" and item["record"] == "4"
                    else [],
                }
                for item in data["records"]
            ]
        )
        validate(response)
        return response

    pipeline.model.ask = ask
    node = SimpleNamespace(name="groups", task="routing", against_target=False)
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await Shuffle(pipeline).run(node, records)
        assert not pipeline.warnings
    elif outcome in {"partial", "linked_partial"}:
        groups = await Shuffle(pipeline).run(node, records)
        accepted = {r.record_id for group in groups for r in group.records}
        expected = {str(i) for i in range(1, 8)}
        assert accepted == expected
        failed = {key for key, state in pipeline.status.items() if state == "failed"}
        assert failed == {r.record_id for r in records} - expected
        assert pipeline.metrics["route_blocked_records"] == len(failed)
        assert pipeline.failures
    elif outcome == "malformed":
        assert await Shuffle(pipeline).run(node, records) == []
        assert set(pipeline.status.values()) == {"failed"}
        assert pipeline.failures and not pipeline.warnings
    else:
        groups = await Shuffle(pipeline).run(node, records)
        assert Counter(r.record_id for g in groups for r in g.records) == Counter(
            r.record_id for r in records
        )
        assert len(groups) == len(records)
        assert not pipeline.warnings
        assert set(pipeline.status.values()) == {"pending"} and not pipeline.failures


@pytest.mark.parametrize("outcome", ["success", "reduce_failure", "merge_failure"])
async def test_output_collisions_merge_successes_and_keep_unrelated_files(runtime, outcome):
    """Bound same-path merges at twenty workers and retain evidence on model failure."""
    pipeline, shards, records = runtime
    pipeline.limits = pipeline.limits.model_copy(update={"merge_concurrency": 20})
    fail_last = outcome == "reduce_failure"
    active = peak = 0
    paths = [f"combined-{i}.txt" for i in range(21)]

    async def ask(stage, system, data, schema, validate, **kwargs):
        nonlocal active, peak
        if stage == "reduce_merge":
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            if outcome == "merge_failure":
                raise ModelCallError("Provider unavailable")
            return schema(content=data["main"]["content"] + data["others"][0]["content"])
        ids = [r["id"] for r in data["inputs"]]
        assert len(ids) == 1
        if fail_last and ids == ["1"]:
            raise ValueError("Synthesis unavailable")
        files = [{"path": f"unique-{ids[0]}.txt", "content": ids[0], "inputs": ids}]
        if ids != ["2"]:
            files.extend({"path": path, "content": ids[0], "inputs": ids} for path in paths)
        response = FileResponse(files=files)
        validate(response)
        return response

    pipeline.model.ask = ask
    refs = await reduce_op.run(
        pipeline,
        SimpleNamespace(name="files", task="reduce"),
        [Group(r.record_id, [r]) for r in records],
    )
    assert active == 0 and peak == (0 if fail_last else 20)
    assert pipeline.metrics["reduce_merge_fallbacks"] == (21 if outcome == "merge_failure" else 0)
    expected = {"unique-0.txt": ["0"], "unique-2.txt": ["2"]}
    expected.update({path: ["0", "1"] if outcome == "success" else ["0"] for path in paths})
    if not fail_last:
        expected["unique-1.txt"] = ["1"]
    assert {shards[ref]["path"]: shards[ref]["source_refs"] for ref in refs} == expected
    assert set(pipeline.artifacts) == set(refs) and bool(pipeline.failures) == fail_last
    assert pipeline.status == {
        "0": "prepared",
        "1": "failed" if fail_last else "prepared",
        "2": "prepared",
    }
    assert len((await merge_op.run(pipeline, refs, partial=fail_last)).operations) == len(expected)


@pytest.mark.parametrize("existing", [False, True])
async def test_skill_package_publication_preserves_attachments_and_checks_revisions(
    runtime, existing
):
    pipeline, _, records = runtime
    pipeline.target = "viking://agent/skills"
    pipeline.skill_target, pipeline.skill_name = True, "compiled"
    path = "compiled/SKILL.md"
    pipeline.contract.required_paths = [path]
    content = "---\nname: compiled\ndescription: Use these procedures.\ntype: index\n---\n# Steps\n"
    if existing:
        pipeline.old[path] = content + "Old instructions\n"
    response = FileResponse(
        files=[
            {
                "path": path,
                "content": content,
                "inputs": [r.record_id for r in records],
                "base_hash": content_hash(pipeline.old[path]) if existing else None,
            },
            {
                "path": "compiled/references/details.md",
                "content": "Evidence",
                "inputs": [r.record_id for r in records],
            },
        ],
    )
    group = Group("skill", records, [pipeline.target + "/" + path] if existing else [])

    async def ask(stage, system, data, schema, validate, **kwargs):
        validate(response)
        return response

    pipeline.model.ask = ask
    refs = await reduce_op.reduce_group(
        pipeline, SimpleNamespace(name="files", task="reduce"), group
    )
    bundle = await merge_op.run(pipeline, refs)
    assert len(bundle.operations) == 2 and not bundle.wiki_uris
    with pytest.raises(ValueError, match="Missing contract-required"):
        await merge_op.run(pipeline, refs[1:], partial=True)
    client = pipeline.client
    client.stat.side_effect = None if existing else NotFoundError("Missing Skill")
    client.stat.return_value = {"isDir": True}
    if existing:
        client.find.return_value = {
            "skills": [{"uri": pipeline.target + "/compiled", "abstract": "Installed Skill"}]
        }
        candidates = await Shuffle(pipeline).candidates("Procedures")
        assert [entry["uri"] for entry in candidates] == [pipeline.target + "/" + path]
    client.get_skill.return_value = {
        "content": pipeline.old.get(path),
        "files": [
            {"path": "assets/keep.bin", "uri": pipeline.target + "/compiled/assets/keep.bin"}
        ],
    }
    client.download_bytes.return_value = b"\x00original attachment\xff"

    async def publish(*args, **kwargs):
        package = Path(args[-1])
        assert (package / "SKILL.md").read_text() == content
        assert (package / "references/details.md").read_text() == "Evidence"
        if existing:
            assert (package / "assets/keep.bin").read_bytes() == b"\x00original attachment\xff"
        return {"root_uri": pipeline.target + "/compiled"}

    client.add_skill.side_effect = client.update_skill.side_effect = publish
    service = object.__new__(BotCompileService)
    result = await service._write_skill_bundle(
        client=client, target_uri=pipeline.target, rendered=bundle, timeout=30
    )
    assert result["updated" if existing else "created"] == [pipeline.target + "/compiled"]
    client.batch_write.assert_not_awaited()
    if existing:
        client.get_skill.return_value["content"] = content + "Concurrent change"
        with pytest.raises(CompileFailure, match="Skill file changed"):
            await service._write_skill_bundle(
                client=client, target_uri=pipeline.target, rendered=bundle, timeout=30
            )
        client.update_skill.assert_awaited_once()
