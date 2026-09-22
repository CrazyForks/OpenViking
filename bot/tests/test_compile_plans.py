"""Exercise typed compile flows, file boundaries, conflict decisions and scoped large-input reads."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.agent.tools.filesystem import ReadFileTool
from vikingbot.compile.models import CompileLimits, SanitizedCompileRequest
from vikingbot.compile.ops import finalize
from vikingbot.compile.pipeline import Pipeline
from vikingbot.compile.pipeline_agent import agent_runner
from vikingbot.compile.plan import DEFAULT_PLAN, Contract, content_hash, parse_plan
from vikingbot.compile.sources import pack_source_batches
from vikingbot.providers.base import LLMResponse, ToolCallRequest
from vikingbot.sandbox.backends.direct import DirectBackend

from openviking_cli.exceptions import NotFoundError

FILE_PLAN = "files = p.map(sources, task=contract.extract)\np.finalize(files, into=target)"


def test_plan_checks_only_used_transforms():
    contract = Contract(extract={"instructions": "Convert", "output": "files"})
    assert [n.op for n in parse_plan(FILE_PLAN, contract)] == ["map", "finalize"]
    with pytest.raises(ValueError):
        parse_plan(DEFAULT_PLAN, contract)
    with pytest.raises(ValueError):
        parse_plan(FILE_PLAN, Contract(extract={"instructions": "Extract"}))
    contract = Contract(
        extract={"instructions": "Extract"},
        reduce={"instructions": "Synthesize"},
        routing={"mode": "all"},
    )
    assert len(parse_plan(DEFAULT_PLAN, contract)) == 4


@pytest.mark.parametrize(
    "mode", ["files", "global", "semantic", "rename", "merge", "failed", "large", "parallel"]
)
async def test_pipeline_flows_and_recoverable_outputs(tmp_path, mode):
    sandbox = DirectBackend(SimpleNamespace(restrict_workspaces={}), None, tmp_path)

    async def stat(uri):
        if uri.endswith("/a.md"):
            return {"size": 3}
        raise NotFoundError(uri)

    client = SimpleNamespace(
        stat=stat,
        download_bytes=AsyncMock(return_value=b"old"),
        find=AsyncMock(return_value={"resources": []}),
        compile_embeddings=AsyncMock(
            side_effect=lambda texts, **kw: {
                "model": "test",
                "vectors": [[1.0, 0.0] for _ in texts],
            }
        ),
    )
    collection = mode in {"global", "semantic"}
    conflict = mode in {"rename", "merge", "failed", "parallel"}
    contract = {
        "extract": {
            "instructions": "Preserve required facts",
            "input_unit": "range" if mode == "semantic" else "file",
            "output": "records" if collection else "files",
        }
    }
    if collection:
        contract.update(
            reduce={"instructions": "Synthesize", "output": "files"},
            routing={"mode": "all"} if mode == "global" else "Jointly process related facts",
        )
    if mode == "semantic":
        contract["options"] = {"output_format": "wiki"}
    proposal = {"contract": contract, "plan": DEFAULT_PLAN if collection else FILE_PLAN}
    stages = []

    def output(data):
        inputs = data["inputs"]
        ids = [i["id"] for i in inputs]
        if "candidates" in data:
            assert not set(pipeline.catalog) & {"same.md", "left.md", "right.md"}
            if mode == "failed":
                return {
                    "files": [{"path": "safe.md", "content": "invalid collision", "inputs": ids}]
                }
            if mode == "rename":
                return {
                    "files": [
                        {"path": f"resolved-{n}.md", "content": c["content"], "inputs": c["inputs"]}
                        for n, c in enumerate(data["candidates"])
                    ]
                }
            path = "other.md" if "merged.md" in data["reserved_paths"] else "merged.md"
            return {"files": [{"path": path, "content": "combined", "inputs": ids}]}
        if not collection:
            assert len({i["payload"]["uri"] for i in inputs}) == 1
            assert len(inputs) > 1  # File boundaries survive character splitting.
        path = "summary.md" if collection else inputs[0]["payload"]["uri"].rsplit("/", 1)[-1]
        if conflict and path != "safe.md":
            path = (
                ("left.md" if path in {"a.md", "b.md"} else "right.md")
                if mode == "parallel"
                else "same.md"
            )
        content = "complete output"
        if mode == "semantic":
            content = (
                "---\ntype: concept\ntitle: Facts\ndescription: Collected facts\n---\n# Facts\n"
            )
        return {"files": [{"path": path, "content": content, "inputs": ids}]}

    async def chat(**kwargs):
        data = json.loads(kwargs["messages"][1]["content"])
        props = kwargs["tools"][0]["function"]["parameters"]["properties"]
        stage = next(key for key in ("contract", "decisions", "records", "files") if key in props)
        stages.append(stage)
        if stage == "contract":
            result = proposal
        elif stage == "decisions":
            assert "SECRET_SKILL" not in kwargs["messages"][0]["content"]
            assert "SECRET_INSTRUCTION" not in kwargs["messages"][0]["content"]
            result = {
                "decisions": [
                    {"record": r["record"], "related": r["candidates"]} for r in data["records"]
                ]
            }
        elif stage == "records":
            result = {
                "records": [
                    {
                        "inputs": [i["id"] for i in data["inputs"]],
                        "payload": {"text": "facts"},
                        "routing_text": "facts",
                    }
                ]
            }
        else:
            result = output(data)
        return LLMResponse(None, [ToolCallRequest("emit", "emit", result, 0)])

    request = SanitizedCompileRequest.model_validate(
        {
            "from": ["viking://resources/in"],
            "to": "viking://resources/out",
            "skill": "viking://agent/skills/convert",
            "instruction": "SECRET_INSTRUCTION",
            "wiki_links": mode == "semantic",
        }
    )
    pipeline = Pipeline(
        client=client,
        sandbox=sandbox,
        provider=SimpleNamespace(chat=chat),
        model="test",
        temperature=0,
        limits=CompileLimits(),
        request=request,
        skill="SECRET_SKILL",
        usage={},
    )

    async def agent_loop(**kwargs):
        assignment = json.loads(kwargs["messages"][1]["content"])
        assert assignment["assignment_file"] == "assignment.json"
        tools = kwargs["tool_registry"]
        submit = tools.get("emit")
        manifest = json.loads(await sandbox.read_file(f"{submit.root}/assignment.json"))
        assert all("text" not in i["payload"] for i in manifest["inputs"])
        source = await tools.get("read_evidence").read(manifest["inputs"][0]["id"])
        assert source["text"]
        await submit.execute(None, **output(submit.data))
        stages.append("agent")

    loop = SimpleNamespace(
        config=None,
        tools=SimpleNamespace(get=lambda name: ReadFileTool() if name == "read_file" else None),
        _run_agent_loop=agent_loop,
    )
    pipeline.model.agent_runner = agent_runner(loop, None, None, pipeline.limits)
    names = [f"{i}.md" for i in range(60)] if mode == "global" else ["a.md", "b.md", "safe.md"]
    if mode == "parallel":
        names.extend(["c.md", "d.md"])
    text = "x\n" * (40000 if mode == "large" else 10)
    batches = pack_source_batches(
        [(f"viking://resources/in/{n}", text) for n in names],
        CompileLimits(source_batch_chars=12000 if mode == "large" else 8),
    )

    async def sources():
        for batch in batches:
            yield batch

    await pipeline.files.put("inputs", {f"viking://resources/in/{n}": "pending" for n in names})
    rendered = await pipeline.run(sources())
    expected = 1 if collection or mode == "failed" else 2 if mode == "merge" else 3
    expected += int(mode == "semantic")  # Wiki navigation is still generated by Finalize.
    assert len(rendered.operations) == expected
    assert bool(pipeline.failures) == (mode == "failed")
    if mode == "global":
        assert len(list(tmp_path.rglob("groups/*.json"))) == 1
        client.compile_embeddings.assert_not_called()
        assert len(pipeline.evidence) > 50
    if not collection:
        client.compile_embeddings.assert_not_called()
        assert "decisions" not in stages
    if mode == "large":
        assert stages.count("agent") == 3
    if mode == "files":
        replacement = next(op for op in rendered.operations if op["uri"].endswith("/a.md"))
        assert replacement["expected_sha256"] == content_hash("old")
    recovered = await finalize.run(pipeline, pipeline.artifacts, partial=bool(pipeline.failures))
    assert recovered.operations == rendered.operations
    await pipeline.write_coverage([op["uri"] for op in rendered.operations])
    coverage = await pipeline.files.get("inputs")
    assert set(coverage.values()) == (
        {"failed", "delivered"} if mode == "failed" else {"delivered"}
    )
