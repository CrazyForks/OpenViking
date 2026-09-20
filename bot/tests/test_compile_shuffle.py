"""Candidate work sets preserve input coverage and version-checked multi-file outputs."""

import asyncio
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.compile.models import CompileFailure, CompileLimits
from vikingbot.compile.pipeline import Pipeline
from vikingbot.compile.plan import (
    Contract,
    FileResponse,
    Group,
    Record,
    RouteResponse,
    Transform,
    content_hash,
)
from vikingbot.compile.service import BotCompileService
from vikingbot.compile.shuffle import Shuffle

from openviking_cli.exceptions import NotFoundError


@pytest.fixture
def runtime(request):
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
    pipeline.load_old = AsyncMock(side_effect=pipeline.old.get)
    return pipeline, shards, records


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
            assert len(data["records"]) <= 4
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
        else:
            assert stage == "reduce" and len(data["inputs"]) == 10 and "path" not in data["group"]
            files = []
            for i, path in enumerate(paths):
                ids = [str(j) for j in range(i, 10, 3)]
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
                response.files[0].base_hash = "stale"
                with pytest.raises(ValueError, match="revision"):
                    validate(response)
                response.files[0].base_hash = content_hash(pipeline.old[paths[0]])
        validate(response)
        return response

    pipeline.model.ask = ask
    groups = await shuffler.run(
        SimpleNamespace(name="groups", task="routing", against_target=historical), records
    )
    assert (
        len(groups) == 1
        and groups[0].records == records
        and groups[0].target_uris == sorted(history)
    )
    assert pipeline.metrics["similarity_records"] == 10
    refs = await pipeline.reduce_sets(SimpleNamespace(name="files", task="reduce"), groups)
    bundle = await pipeline.merge(refs)
    assert len(bundle.operations) == 3 and len(bundle.updated) == (2 if historical else 0)
    assert {shards[ref]["path"]: shards[ref]["source_refs"] for ref in refs} == {
        path: [str(j) for j in range(i, 10, 3)] for i, path in enumerate(paths)
    }
    assert all(op.get("expected_sha256") for op in bundle.operations if op["mode"] == "replace")
    assert set(pipeline.status.values()) == {"prepared"} and not pipeline.failures
    if not historical:
        pipeline.old[paths[0]] = "Existing outside the supplied history"
        guarded = await pipeline.merge(refs)
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
        expected = {"4", "5", "6", "7"}
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


@pytest.mark.parametrize("fail_last", [False, True])
async def test_output_collisions_merge_successes_and_keep_unrelated_files(runtime, fail_last):
    pipeline, shards, records = runtime
    pipeline.limits = pipeline.limits.model_copy(update={"merge_concurrency": 1})

    async def ask(stage, system, data, schema, validate, **kwargs):
        if stage == "reduce_merge":
            return schema(content=data["main"]["content"] + data["others"][0]["content"])
        ids = [r["id"] for r in data["inputs"]]
        assert len(ids) == 1
        if fail_last and ids == ["1"]:
            raise ValueError("Synthesis unavailable")
        files = [{"path": f"unique-{ids[0]}.txt", "content": ids[0], "inputs": ids}]
        if ids != ["2"]:
            files.append({"path": "combined.txt", "content": ids[0], "inputs": ids})
        response = FileResponse(files=files)
        validate(response)
        return response

    pipeline.model.ask = ask
    refs = await pipeline.reduce_sets(
        SimpleNamespace(name="files", task="reduce"), [Group(r.record_id, [r]) for r in records]
    )
    expected = {"unique-0.txt": ["0"], "unique-2.txt": ["2"]}
    expected["combined.txt"] = ["0"] if fail_last else ["0", "1"]
    if not fail_last:
        expected["unique-1.txt"] = ["1"]
    assert {shards[ref]["path"]: shards[ref]["source_refs"] for ref in refs} == expected
    assert set(pipeline.artifacts) == set(refs) and bool(pipeline.failures) == fail_last
    assert pipeline.status == {
        "0": "prepared",
        "1": "failed" if fail_last else "prepared",
        "2": "prepared",
    }
    assert len((await pipeline.merge(refs, partial=fail_last)).operations) == len(expected)


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
    refs = await pipeline.reduce_group(SimpleNamespace(name="files", task="reduce"), group)
    bundle = await pipeline.merge(refs)
    assert len(bundle.operations) == 2 and not bundle.wiki_uris
    with pytest.raises(ValueError, match="Missing contract-required"):
        await pipeline.merge(refs[1:], partial=True)
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
