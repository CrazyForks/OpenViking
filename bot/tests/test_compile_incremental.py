"""Compile excludes old source bodies before preparing model input."""

import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.compile.models import CompileLimits
from vikingbot.compile.pipeline import Pipeline
from vikingbot.compile.pipeline_agent import EmitResult
from vikingbot.compile.pipeline_io import JsonModel
from vikingbot.compile.plan import (
    Contract,
    FileResponse,
    Group,
    PlanProposal,
    Record,
    RecordResponse,
    Transform,
    content_hash,
    parse_plan,
    result_schema,
)
from vikingbot.compile.renderer import RenderedBundle
from vikingbot.compile.service import BotCompileService, _compile_time
from vikingbot.providers.base import LLMResponse, ToolCallRequest


async def test_compile_client_serializes_skip_conflicts_for_content_api():
    from openviking_sdk import AsyncHTTPClient
    from vikingbot.openviking_mount.ov_server import VikingClient

    from openviking.server.routers.content import BatchWriteRequest

    client = object.__new__(VikingClient)
    client.client = AsyncHTTPClient(url="http://localhost:1933")
    client.client._request = AsyncMock(return_value=object())
    expected = {
        "created": [],
        "conflicts": [
            {"uri": "viking://resources/out/page.txt", "code": "CONFLICT", "message": "Changed"}
        ],
    }
    client.client._handle_response_data = lambda response: {"result": expected}
    result = await client.batch_write(
        root_uri="viking://resources/out",
        operations=[
            {
                "uri": "viking://resources/out/page.txt",
                "content": "Draft",
                "mode": "replace",
                "expected_sha256": content_hash("Original"),
            }
        ],
        wait=False,
        skip_conflicts=True,
    )
    request = BatchWriteRequest.model_validate(client.client._request.call_args.kwargs["json"])
    assert request.skip_conflicts is True
    assert request.operations[0].expected_sha256 == content_hash("Original")
    assert result == expected


@pytest.mark.parametrize("existing", [None, "Original"])
async def test_merge_defers_resource_conflicts_and_unchanged_checks_to_locked_write(existing):
    pipeline = Pipeline(
        client=AsyncMock(),
        sandbox=AsyncMock(),
        provider=SimpleNamespace(),
        model="test",
        temperature=0,
        limits=CompileLimits(),
        request=SimpleNamespace(to="viking://resources/out", skill="viking://agent/skills/test"),
        skill="Skill",
        usage={},
    )
    pipeline.contract = SimpleNamespace(required_paths=[], output_format="files")
    pipeline.evidence = {"source": {"uri": "viking://resources/source"}}
    pipeline.old = {"page.txt": "Original"}
    artifact = {
        "path": "page.txt",
        "content": "Original",
        "sha256": content_hash("Original"),
        "base_hash": content_hash(existing) if existing is not None else None,
        "owner": "group",
        "source_refs": ["source"],
    }
    pipeline.files.get = AsyncMock(return_value=artifact)
    pipeline.files.put = AsyncMock()
    rendered = await pipeline.merge(["artifacts/page"])
    assert not rendered.unchanged
    assert len(rendered.operations) == 1
    operation = rendered.operations[0]
    assert operation["mode"] == ("create" if existing is None else "replace")
    assert operation.get("expected_sha256") == artifact["base_hash"]


@pytest.mark.parametrize("all_conflict", [False, True])
async def test_compile_partial_publication_reports_conflicts_and_only_acknowledges_written_pages(
    monkeypatch, all_conflict
):
    root = "viking://resources/out"
    good, changed = root + "/good.md", root + "/changed.md"
    rendered = RenderedBundle(
        operations=[{"uri": uri, "content": "Draft", "mode": "create"} for uri in [good, changed]],
        created=[good, changed],
        wiki_uris=[good, changed],
    )
    pipeline = SimpleNamespace(
        model=SimpleNamespace(),
        run=AsyncMock(return_value=rendered),
        warnings=[],
        metrics={},
        files=SimpleNamespace(put=AsyncMock(), get=AsyncMock(return_value={})),
        write_coverage=AsyncMock(),
    )
    monkeypatch.setattr("vikingbot.compile.service.Pipeline", lambda **kwargs: pipeline)
    service = object.__new__(BotCompileService)
    service.agent_loop = SimpleNamespace(provider=SimpleNamespace(), model="test", temperature=0)
    service._model_slots = None
    service.limits = CompileLimits()
    service._set_state = AsyncMock()
    task = SimpleNamespace(status="running", meta={})

    async def update(task_id, change):
        change(task)

    service.store = SimpleNamespace(
        get=AsyncMock(return_value=task), update=AsyncMock(side_effect=update)
    )
    client = AsyncMock()
    conflicts = [
        {"uri": uri, "code": "CONFLICT", "message": "Content revision changed"}
        for uri in ([good, changed] if all_conflict else [changed])
    ]
    published = [] if all_conflict else [good]
    client.batch_write.return_value = {
        "created": published,
        "updated": [],
        "unchanged": [],
        "conflicts": conflicts,
    }
    await service._run_pipeline(
        task_id="compile",
        request=SimpleNamespace(
            from_=["viking://resources/source"], to=root, skill="viking://agent/skills/test"
        ),
        client=client,
        sandbox=AsyncMock(),
        skill_text="Skill",
        source_files=[],
        usage={},
    )
    assert client.batch_write.call_args.kwargs["skip_conflicts"] is True
    pipeline.write_coverage.assert_awaited_once_with(published)
    assert task.result.created == published
    assert task.result.conflicts == conflicts
    assert task.result.page_count == len(published)
    assert task.status == "failed" and task.stage == "salvaged"
    assert task.error.code == "COMPILE_INCOMPLETE"


@pytest.mark.parametrize(
    "cutoff, expected", [(None, [0, 1, 2, 3]), ("1970-01-01T08:00:01+08:00", [1, 2, 3]), (3, [3])]
)
async def test_source_filter(cutoff, expected):
    service = object.__new__(BotCompileService)
    service.limits = CompileLimits()
    root = "viking://resources/source"
    client = AsyncMock()
    client.stat.return_value = {"isDir": True, "modTime": 0}
    client.list_resources.return_value = [
        {"uri": f"{root}/{i}.md", "modTime": time} for i, time in enumerate([0, 1, 2, None])
    ]
    client.read_raw.return_value = "Source content"
    cutoff = _compile_time(cutoff) if cutoff is not None else None
    files = await service._list_source_files(client, [root, root], cutoff=cutoff)
    assert files == [f"{root}/{i}.md" for i in expected]
    await service._prepare_source_batches(client, [root], files=files)
    assert [call.args[0] for call in client.read_raw.await_args_list] == files
    client.list_resources.return_value = [{"uri": root + "/old.md", "modTime": 0}]
    assert await service._list_source_files(client, [root], cutoff=_compile_time(1)) == []


async def test_delivery_requires_every_branch_and_acknowledged_final_bytes():
    pipeline = object.__new__(Pipeline)
    pipeline.target = "viking://resources/out"
    source = "viking://resources/input/source.md"
    pipeline.evidence = {
        "source": {"uri": source, "hash": "source-hash", "start_char": 0, "end_char": 10}
    }
    pipeline.records = {
        key: Record(key, "", ["source"], "", {}, parents)
        for key, parents in [("source", []), ("accepted", ["source"]), ("missing", ["source"])]
    }
    pipeline.status = {"source": "pending", "accepted": "prepared", "missing": "failed"}
    shards = {
        "inputs": {source: "pending"},
        "merge": {"outputs": {"page.md": {"sha256": "final-byte-hash", "source_refs": ["source"]}}},
    }
    pipeline.files = AsyncMock()
    pipeline.files.get.side_effect = lambda path: shards.get(path)
    pipeline.files.put.side_effect = lambda path, value: shards.update({path: value})
    await pipeline.write_coverage([pipeline.target + "/page.md"])
    assert shards["inputs"][source] == "failed"
    assert shards["coverage"]["inputs"][source]["outputs"][0]["delivered"]
    pipeline.status.update(source="pending", missing="excluded")
    await pipeline.write_coverage()
    assert shards["inputs"][source] == "prepared"
    await pipeline.write_coverage([pipeline.target + "/page.md"])
    assert shards["inputs"][source] == "delivered"
    assert shards["coverage"]["counts"] == {"delivered": 1}
    assert shards["coverage"]["inputs"][source]["outputs"][0]["sha256"] == "final-byte-hash"


@pytest.mark.parametrize("repair", [True, False])
@pytest.mark.parametrize("malformed", [True, False])
async def test_rejected_result_can_be_edited_without_resubmitting_its_body(repair, malformed):
    source = SimpleNamespace(record_id="source")
    sandbox = AsyncMock()
    tool = EmitResult(
        FileResponse,
        lambda result: Pipeline.coverage(
            [source], [i for draft in result.files for i in draft.inputs], []
        ),
        sandbox,
        "child",
        metrics=Counter(),
    )
    candidate = {
        "files": [
            {"path": "page.md", "content": "Complete evidence\n" * 1000, "inputs": ["unknown"]}
        ],
    }
    raw = json.dumps(candidate, ensure_ascii=False)[:-1] + ",}"
    feedback = await tool.execute(None, **({"raw": raw} if malformed else candidate))
    path, saved = sandbox.write_file.call_args.args
    assert feedback.startswith("Error:") and tool.result is None
    assert path.startswith("child/rejected-")
    assert json.loads(saved) == candidate
    assert "result_ref" in feedback and tool.metrics["repairs"] == 1
    if repair:
        candidate["files"][0]["inputs"] = ["source"]
    sandbox.read_file_bytes.return_value = (json.dumps(candidate) if repair else saved).encode()
    feedback = await tool.execute(None, result_ref=path.removeprefix("child/"))
    assert (tool.result is not None) == repair
    assert tool.failures == (1 if repair else 2)
    assert sandbox.write_file.await_count == 1
    if repair:
        assert tool.result.files[0].content == candidate["files"][0]["content"]


@pytest.mark.parametrize("value", [None, [], 1, {"result_ref": "../outside.json"}])
async def test_invalid_result_references_are_not_staged(value):
    sandbox = AsyncMock()
    tool = EmitResult(FileResponse, None, sandbox, "child")
    assert (await tool.execute(None, raw=json.dumps(value))).startswith("Error:")
    sandbox.write_file.assert_not_awaited()
    sandbox.read_file_bytes.assert_not_awaited()
    assert tool.result is None


@pytest.mark.parametrize("inputs", [None, []])
def test_content_files_require_supporting_input_lineage(inputs):
    pipeline = object.__new__(Pipeline)
    pipeline.owners = {}
    pipeline.skill_target = False
    pipeline.contract = SimpleNamespace(output_format="files")
    source = Record("source", "", ["source"], "", {}, [])
    group = Group("group", [source])
    draft = {"path": "page.md", "content": "Content", "inputs": inputs}
    with pytest.raises(ValueError, match="inputs"):
        FileResponse.model_validate({"files": [draft]})
    draft["inputs"] = [source.record_id]
    response = FileResponse.model_validate({"files": [draft]})
    pipeline.validate_files(response, group, [source], {})


def test_minimal_planner_output_expands_to_valid_flow_and_roundtrips():
    raw = {
        "contract": {
            "extract": {"instructions": "Extract"},
            "reduce": {"instructions": "Write"},
            "routing": "Identity",
        }
    }
    original = json.dumps(raw)
    proposal = PlanProposal.model_validate(raw)
    assert [node.op for node in parse_plan(proposal.plan, proposal.contract)] == [
        "map",
        "shuffle",
        "reduce",
        "merge",
    ]
    assert proposal.contract.reduce.output == "files"
    assert PlanProposal.model_validate(proposal.model_dump()) == proposal
    assert json.dumps(raw) == original
    schema = result_schema(PlanProposal, {})
    contract = schema["$defs"]["Contract"]
    assert set(contract["required"]) == {"extract", "reduce", "routing"}
    assert set(contract["properties"]) == {"extract", "reduce", "routing", "distinguish", "options"}


def test_planner_options_preserve_multilevel_synthesis_and_overflow():
    raw = {
        "contract": {
            "extract": {"instructions": "Extract"},
            "reduce": {"instructions": "Aggregate", "output": "records"},
            "routing": "Dimension",
            "distinguish": ["scope"],
            "options": {
                "synthesize": {"instructions": "Write", "output": "files", "execution": "agent"},
                "final_routing": "Final file",
                "combine": {"instructions": "Preserve joint evidence"},
                "overflow": "structured",
                "preserve": ["Exceptions"],
                "validation": "Citations",
                "output_format": "wiki",
                "required_paths": ["report.md"],
            },
        },
        "plan": "records = p.map(sources, task=contract.extract)\ngroups = p.shuffle(records, by=contract.routing)\nrules = p.reduce(groups, task=contract.reduce)\noutputs = p.shuffle(rules, by=contract.final_routing)\nfiles = p.reduce(outputs, task=contract.synthesize)\np.merge(files, into=target)",
    }
    proposal = PlanProposal.model_validate(raw)
    assert len(parse_plan(proposal.plan, proposal.contract)) == 6
    for key, value in raw["contract"]["options"].items():
        actual = getattr(proposal.contract, key)
        assert (
            all(getattr(actual, k) == v for k, v in value.items())
            if isinstance(value, dict)
            else actual == value
        )
    assert PlanProposal.model_validate(proposal.model_dump()) == proposal


@pytest.mark.parametrize(
    "options",
    [
        [],
        {"unknown": True},
        {"routing": "Override"},
        {"version": 2},
        {"preserve": ["Conflicting requirement"]},
        {"unsupported": ["Missing capability"]},
        {"overflow": "structured"},
    ],
)
def test_invalid_planner_options_cannot_bypass_contract_validation(options):
    with pytest.raises(ValueError):
        Contract.model_validate(
            {
                "extract": {"instructions": "Extract"},
                "reduce": {"instructions": "Write"},
                "routing": "Identity",
                "preserve": [],
                "options": options,
            }
        )


@pytest.mark.parametrize("custom_plan", [True, False])
async def test_planner_cleans_reserved_fields_and_repairs_invalid_dataflow(custom_plan):
    contract = Contract(
        extract=Transform(instructions="Extract"),
        reduce=Transform(instructions="Write", output="files"),
        routing="Identity",
        preserve=["Evidence"],
        validation="Sources",
    ).model_dump()
    prefix = "records = p.map(sources, task=contract.extract)\ngroups = p.shuffle(records, by=contract.routing)\nchanges = p.reduce(groups, task=contract.reduce)\n"
    malformed = {
        "contract": {
            **contract,
            "options": {"synthesize": {"instructions": "Check", "fields": ["ready_content"]}},
        },
        "plan": prefix
        + "pages = p.map(changes, task=contract.synthesize)\np.merge(pages, into=target)",
    }
    malformed["contract"].pop("synthesize")
    repaired = {"contract": contract, "plan": prefix + "p.merge(changes, into=target)"}
    if not custom_plan:
        malformed.pop("plan")
        repaired.pop("plan")
    model = JsonModel(AsyncMock(), "test", 0, AsyncMock(), CompileLimits(), {}, Counter())
    model.call = AsyncMock(
        side_effect=[
            LLMResponse(None, [ToolCallRequest(str(i), "emit", value, 0)])
            for i, value in enumerate([malformed, repaired])
        ]
    )
    result = await model.direct(
        "plan", "Skill", {}, PlanProposal, lambda p: parse_plan(p.plan, p.contract), "test"
    )
    if not custom_plan:
        assert result.contract.synthesize.fields == {}
        assert model.call.await_count == 1
        assert parse_plan(result.plan, result.contract)
        return
    feedback = model.call.call_args.args[1][-1]["content"]
    assert "ready_content" in feedback
    assert ("Invalid dataset type for map: files" in feedback) == custom_plan
    assert "Invalid plan syntax" not in feedback
    assert parse_plan(result.plan, result.contract) and model.call.await_count == 2


@pytest.mark.parametrize("ready", [False, True])
async def test_map_preserves_scope_and_accepts_ready_body_without_duplicate_evidence(ready):
    pipeline = Pipeline(
        client=AsyncMock(),
        sandbox=AsyncMock(),
        provider=SimpleNamespace(),
        model="test",
        temperature=0,
        limits=CompileLimits(),
        request=SimpleNamespace(to="viking://resources/out", skill="viking://agent/skills/test"),
        skill="Skill",
        usage={},
    )
    transform = Transform(instructions="Extract")
    pipeline.contract = Contract(
        extract=transform,
        reduce=Transform(instructions="Write", output="files"),
        routing="Identity",
        distinguish=["scope"],
        preserve=["Facts"],
        validation="Evidence",
    )
    source = Record("source", "sources/source", ["source"], "", {}, [])
    pipeline.evidence = {"source": {"uri": "viking://resources/source"}}
    pipeline.files.get = AsyncMock(
        return_value={"uri": "viking://resources/source", "text": "Original"}
    )
    pipeline.files.put = AsyncMock()
    scope = "Full applicability boundary; " * 10

    async def ask(stage, system, data, schema, validate, **kwargs):
        value = RecordResponse.model_validate(
            {
                "records": [
                    {
                        "inputs": ["source"],
                        "payload": {"text": None if ready else "Evidence"},
                        "scope": {"scope": scope},
                        "routing_text": "Identity",
                        "ready_path": "proposed.md",
                        "ready_content": "Complete evidence" if ready else None,
                    }
                ]
            }
        )
        validate(value)
        return value

    pipeline.model.ask = ask
    records = await pipeline.transform("map", transform, [source])
    assert bool(records[0].ready_ref) == ready and records[0].scope["scope"] == scope
    assert not pipeline.artifacts
