"""Exercise partial routing responses through the real model parser and Shuffle scheduler."""

import asyncio
import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from vikingbot.compile.models import CompileLimits
from vikingbot.compile.ops import shuffle as shuffle_op
from vikingbot.compile.pipeline_io import JsonModel
from vikingbot.compile.plan import Node, Record
from vikingbot.config.schema import CompileConfig
from vikingbot.providers.base import LLMResponse, ToolCallRequest


class MemoryFiles:
    """Isolate checkpoint writes while retaining the production model response parser."""

    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def put(self, key, value):
        self.data[key] = value


def setup_runtime(monkeypatch, answer, *, count=4, batch_size=4, neighbours=None):
    """Build deterministic candidate recall; only the provider's response is simulated."""
    files, metrics, calls = MemoryFiles(), Counter(), []

    async def chat(**kwargs):
        request = json.loads(kwargs["messages"][1]["content"])
        calls.append(request)
        raw = answer(request, len(calls))
        if isinstance(raw, BaseException):
            raise raw
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest("result", "emit", raw, 0)],
            finish_reason="tool_calls",
        )

    limits = CompileLimits(shuffle_batch_size=batch_size, shuffle_concurrency=2)
    model = JsonModel(SimpleNamespace(chat=chat), "test", 0, files, limits, {}, metrics)
    records = [
        Record(str(i), f"payloads/{i}", [str(i)], f"subject {i}", {}, []) for i in range(count)
    ]
    runtime = SimpleNamespace(
        model=model,
        files=files,
        metrics=metrics,
        limits=limits,
        system="Routing rules",
        contract=SimpleNamespace(routing="Compare evidence", distinguish={}),
        evidence={str(i): {"uri": f"viking://resources/input/{i}"} for i in range(count)},
        status={r.record_id: "pending" for r in records},
        failures=[],
    )
    shuffle = shuffle_op.Shuffle(runtime)
    shuffle.vectors = AsyncMock(return_value=[[1.0, float(i)] for i in range(count)])
    candidates = neighbours or [[j for j in range(count) if i != j][:5] for i in range(count)]
    monkeypatch.setattr(shuffle_op, "top_candidates", lambda *args: candidates)
    node = Node("groups", "shuffle", "records", "routing")
    return runtime, shuffle, node, records, calls


def valid(request, _):
    return {
        "decisions": [
            {"record": r["record"], "related": [], "history": []} for r in request["records"]
        ]
    }


@pytest.mark.parametrize("size,expected", [(1, 8), (4, 2), (8, 1)])
async def test_batch_size_and_independent_primary_coverage(monkeypatch, size, expected):
    r, shuffle, node, records, calls = setup_runtime(monkeypatch, valid, count=8, batch_size=size)
    groups = await shuffle.run(node, records)
    assert len(calls) == expected
    assert Counter(x["record"] for c in calls for x in c["records"]) == Counter(map(str, range(8)))
    assert len(groups) == 8  # Empty decisions are successful independent records.
    for call in calls:
        primaries = {x["record"] for x in call["records"]}
        assert primaries.isdisjoint(call["candidates"])
        assert all(
            set(x["candidates"]) <= primaries | call["candidates"].keys() for x in call["records"]
        )
    assert not r.failures
    assert not any(key.startswith("cache/") for key in r.files.data)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "schema", "candidate", "history"])
async def test_only_invalid_primary_retries(monkeypatch, fault):
    def answer(request, attempt):
        result = valid(request, attempt)
        if attempt == 1:
            bad = result["decisions"][1]
            if fault == "missing":
                result["decisions"].pop(1)
            elif fault == "duplicate":
                result["decisions"].append(dict(bad))
            elif fault == "schema":
                bad["related"] = 42
            elif fault == "candidate":
                bad["related"] = ["3"]  # Visible, but not primary 1's candidate.
            else:
                bad["history"] = ["viking://resources/history/0"]
        else:
            assert all(f"routes/groups-{i}" in r.files.data for i in [0, 2, 3])
            assert request["records"][0]["previous_error"]
        return result

    r, shuffle, node, records, calls = setup_runtime(
        monkeypatch,
        answer,
        neighbours=[[1], [0], [3], [2]],
    )
    shuffle.candidates = AsyncMock(
        side_effect=lambda text, **kw: [{"uri": f"viking://resources/history/{text.split()[-1]}"}]
    )
    node = Node("groups", "shuffle", "records", "routing", True)
    assert len(await shuffle.run(node, records)) == 4
    assert [[x["record"] for x in c["records"]] for c in calls] == [["0", "1", "2", "3"], ["1"]]
    assert r.metrics["route_record_retries"] == 1
    assert not r.failures


async def test_failed_records_regroup_then_fall_back_to_singletons(monkeypatch):
    def answer(request, _):
        result = valid(request, 0)
        for decision in result["decisions"]:
            if decision["record"] in {"1", "6"}:
                decision["related"] = ["unknown"]
        return result

    r, shuffle, node, records, calls = setup_runtime(monkeypatch, answer, count=8)
    groups = await shuffle.run(node, records)
    batches = [[x["record"] for x in c["records"]] for c in calls]
    assert batches[:2] == [["0", "1", "2", "3"], ["4", "5", "6", "7"]]
    assert batches[2] == ["1", "6"]
    assert sorted(batches[3:]) == [["1"], ["6"]]
    assert len(groups) == 6
    assert r.metrics["route_blocked_records"] == 2
    assert r.status["1"] == r.status["6"] == "failed"
    assert r.files.data["jobs/groups-1"]["attempt"] == 3
    assert len(r.failures) == 1


async def test_unparseable_envelope_retries_once_per_round(monkeypatch):
    def answer(request, attempt):
        return {"decisions": "invalid"} if attempt == 1 else valid(request, attempt)

    r, shuffle, node, records, calls = setup_runtime(monkeypatch, answer)
    assert len(await shuffle.run(node, records)) == 4
    assert len(calls) == 2
    assert all("previous_error" in x for x in calls[1]["records"])
    assert r.metrics["repairs"] == 0  # No hidden whole-batch repair inside JsonModel.


async def test_oversized_batch_splits_and_cancellation_propagates(monkeypatch):
    r, shuffle, node, records, calls = setup_runtime(monkeypatch, valid)
    r.model.fits = lambda system, data, schema: len(data["records"]) <= 2
    assert len(await shuffle.run(node, records)) == 4
    assert [len(c["records"]) for c in calls] == [2, 2]

    r, shuffle, node, records, calls = setup_runtime(
        monkeypatch, lambda *_: asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await shuffle.run(node, records)
    assert len(calls) == 1
    assert all(r.files.data[f"jobs/groups-{i}"]["status"] == "pending" for i in range(4))
    assert not any(key.startswith("groups/") for key in r.files.data)


@pytest.mark.parametrize("model", [CompileConfig, CompileLimits])
def test_batch_size_configuration(model):
    assert model().shuffle_batch_size == 4
    assert model(shuffle_batch_size=8).shuffle_batch_size == 8
    for value in [0, -1, True, "4", None]:
        with pytest.raises(ValidationError):
            model(shuffle_batch_size=value)


async def test_candidate_is_still_judged_with_its_own_neighbours(monkeypatch):
    def answer(request, attempt):
        result = valid(request, attempt)
        for decision in result["decisions"]:
            if decision["record"] == "0":
                decision["related"] = ["1"]
            elif decision["record"] == "1":
                decision["related"] = ["5"]
        return result

    r, shuffle, node, records, calls = setup_runtime(
        monkeypatch,
        answer,
        count=6,
        neighbours=[[1], [0, 5], [3], [2], [5], [1, 4]],
    )
    await shuffle.run(node, records)
    assert r.files.data["routes/groups-0"]["related"] == ["1"]
    assert r.files.data["routes/groups-1"]["related"] == ["5"]
    assert Counter(x["record"] for c in calls for x in c["records"]) == Counter(map(str, range(6)))
    assert not r.failures


async def test_recall_failure_does_not_discard_other_primaries(monkeypatch):
    r, shuffle, _, records, calls = setup_runtime(monkeypatch, valid)
    seen = Counter()

    async def recall(text, **kwargs):
        seen[text] += 1
        if text == "subject 1" and seen[text] == 1:
            raise OSError("temporary recall error")
        return []

    shuffle.candidates = recall
    await shuffle.run(Node("groups", "shuffle", "records", "routing", True), records)
    assert [[x["record"] for x in c["records"]] for c in calls] == [["0", "2", "3"], ["1"]]
    assert not r.failures
