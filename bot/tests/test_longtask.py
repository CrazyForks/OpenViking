"""Host invariants plus opt-in contract tests against the real pinned LoopX CLI."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.agent.tools.base import ToolContext
from vikingbot.agent.tools.registry import ToolRegistry
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import Config, SessionKey
from vikingbot.longtask.host_store import HostStore
from vikingbot.longtask.loopx_client import (
    LOOPX_VERSION,
    WORKFLOW_IDS,
    LoopXClient,
    LoopXError,
    load_workflows,
    validate_installation,
)
from vikingbot.longtask.runner import LongTaskService


def test_longtask_disabled_by_default():
    assert Config().longtask.enabled is False


def test_store_dedup_and_conflicting_intent(tmp_path):
    store = HostStore(tmp_path / "host.db")
    first, created = store.create("user", "chat", "request", "goal", {})
    second, again = store.create("user", "chat", "request", "goal", {})
    assert created and not again and first["task_id"] == second["task_id"]
    assert first["authorized"] and store.due()[0]["task_id"] == first["task_id"]
    with pytest.raises(ValueError, match="different objective"):
        store.create("user", "chat", "request", "changed", {})
    store.close()


def test_restart_never_replays_unknown_operations(tmp_path):
    path = tmp_path / "host.db"
    store = HostStore(path)
    row, _ = store.create("user", "chat", "request", "goal", {})
    task_id = row["task_id"]
    store.update(task_id, authorized=1, inflight="turn", rounds=4)
    operation = store.begin_operation(task_id, "turn", "tool", {"name": "send"})
    store.close()
    store = HostStore(path)
    store.recover()
    assert not store.get(task_id)["authorized"]
    assert store.get(task_id)["rounds"] == 4
    assert store.unresolved(task_id) == [operation]
    assert store.due() == []
    store.close()


def test_missing_workflows_fail_closed(tmp_path):
    with pytest.raises(LoopXError, match="workflow-skills"):
        load_workflows(tmp_path)


def _write_workflows(skills_root):
    for skill_id in WORKFLOW_IDS:
        directory = skills_root / skill_id
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"Instructions for {skill_id}")
        (directory / ".loopx-skill-version.json").write_text(
            json.dumps({"loopx_version": LOOPX_VERSION, "skill_id": skill_id})
        )


@pytest.fixture
def installed_loopx_version(monkeypatch):
    monkeypatch.setattr(
        "vikingbot.longtask.loopx_client.importlib.metadata.version", lambda _: LOOPX_VERSION
    )


@pytest.mark.asyncio
async def test_first_start_prepares_workflows_once(tmp_path, monkeypatch, installed_loopx_version):
    skills_root = tmp_path / "skills"
    commands = []

    async def call(self, *args):
        commands.append(args)
        if args[0] == "doctor":
            return {"ok": True, "typescript_control_plane": {"ready": True}}
        assert args == ("workflow-skills", "--install", "--skills-dir", str(skills_root))
        _write_workflows(skills_root)
        return {"ok": True}

    monkeypatch.setattr(LoopXClient, "call", call)
    first = await validate_installation(tmp_path)
    assert set(first) == set(WORKFLOW_IDS)
    # Existing files are reused, including their contents, with no second install.
    (skills_root / "loopx" / "SKILL.md").write_text("Existing instructions")
    second = await validate_installation(tmp_path)
    assert second["loopx"] == "Existing instructions"
    assert [args[0] for args in commands] == ["doctor", "workflow-skills", "doctor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "version", "empty"])
async def test_existing_invalid_workflows_are_not_reinstalled(
    tmp_path, monkeypatch, installed_loopx_version, invalid
):
    skills_root = tmp_path / "skills"
    _write_workflows(skills_root)
    entry = skills_root / "loopx"
    if invalid == "missing":
        (entry / "SKILL.md").unlink()
    elif invalid == "version":
        (entry / ".loopx-skill-version.json").write_text(
            json.dumps({"loopx_version": "wrong-version", "skill_id": "loopx"})
        )
    else:
        (entry / "SKILL.md").write_text("")
    call = AsyncMock(return_value={"ok": True, "typescript_control_plane": {"ready": True}})
    monkeypatch.setattr(LoopXClient, "call", call)
    with pytest.raises(LoopXError):
        await validate_installation(tmp_path)
    call.assert_awaited_once_with("doctor", "--installation-only")


@pytest.mark.asyncio
async def test_unready_node_does_not_prepare_skills(tmp_path, monkeypatch, installed_loopx_version):
    call = AsyncMock(return_value={"ok": True, "typescript_control_plane": {"ready": False}})
    monkeypatch.setattr(LoopXClient, "call", call)
    with pytest.raises(LoopXError, match="Node"):
        await validate_installation(tmp_path)
    call.assert_awaited_once_with("doctor", "--installation-only")
    assert not (tmp_path / "skills").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cli_error", "unconfirmed", "no_files"])
async def test_preparation_failure_stops_startup(
    tmp_path, monkeypatch, installed_loopx_version, failure
):
    async def call(self, *args):
        if args[0] == "doctor":
            return {"ok": True, "typescript_control_plane": {"ready": True}}
        if failure == "cli_error":
            raise LoopXError("Preparation failed")
        return {} if failure == "unconfirmed" else {"ok": True}

    monkeypatch.setattr(LoopXClient, "call", call)
    with pytest.raises(LoopXError):
        await validate_installation(tmp_path)


def test_missing_cli_is_not_replaced_with_another_entrypoint(tmp_path, monkeypatch):
    from pathlib import Path

    from vikingbot.longtask import loopx_client

    distribution = SimpleNamespace(
        files=[Path("bin/loopx")], locate_file=lambda item: tmp_path / item
    )
    monkeypatch.setattr(loopx_client.importlib.metadata, "distribution", lambda _: distribution)
    with pytest.raises(LoopXError, match="official console script"):
        loopx_client.installed_cli()


def _service(tmp_path):
    config = Config(storage_workspace=str(tmp_path))
    agent = SimpleNamespace(config=config, tools=ToolRegistry(config), bus=MessageBus())
    return LongTaskService(agent)


def _context(user="owner"):
    return ToolContext(
        sender_id=user, session_key=SessionKey(type="test", channel_id="bot", chat_id="chat")
    )


@pytest.mark.asyncio
async def test_only_one_host_can_prepare_workflows(tmp_path, monkeypatch):
    validate = AsyncMock(return_value={})
    monkeypatch.setattr("vikingbot.longtask.runner.validate_installation", validate)
    first, second = _service(tmp_path), _service(tmp_path)
    try:
        await first.initialize()
        with pytest.raises(RuntimeError, match="Another long-task host"):
            await second.initialize()
        validate.assert_awaited_once_with(
            first.root, timeout=first.config.longtask.cli_timeout_seconds
        )
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_owner_check_and_pause_revokes_before_cli_failure(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.store = HostStore(service.root / "host.db")
    task = await service.create(_context(), "goal", "req")
    task_id = task["task_id"]
    with pytest.raises(ValueError, match="Unknown long task"):
        await service.control(_context("someone_else"), task_id, "status")
    service.store.update(task_id, initialized=1)

    async def fail(*args):
        raise LoopXError("stop failed")

    monkeypatch.setattr(service, "_client", lambda _: SimpleNamespace(call=fail))
    with pytest.raises(LoopXError, match="stop failed"):
        await service.control(_context(), task_id, "pause")
    assert not service.store.get(task_id)["authorized"]
    await service.close()


@pytest.mark.asyncio
async def test_provider_budget_counts_stream_and_chat_separately(tmp_path):
    from vikingbot.longtask.provider import BudgetedProvider
    from vikingbot.longtask.runner import Turn
    from vikingbot.providers.base import LLMResponse

    service = _service(tmp_path)
    service.config.longtask.model_calls_per_round = 1
    service.store = HostStore(service.root / "host.db")
    task = await service.create(_context(), "goal", "req")
    turn = Turn(service, task["task_id"], "turn")
    delegate = SimpleNamespace(
        api_key=None,
        api_base=None,
        chat=AsyncMock(return_value=LLMResponse(content="ok", usage={"total_tokens": 3})),
    )
    provider = BudgetedProvider(delegate, turn)
    await provider.chat(messages=[])
    with pytest.raises(RuntimeError, match="model invocation budget"):
        await provider.chat(messages=[])
    assert delegate.chat.await_count == 1
    assert not service.store.unresolved(task["task_id"])
    await service.close()


@pytest.mark.asyncio
async def test_failed_submission_is_not_converted_into_success(tmp_path):
    from vikingbot.longtask.runner import Turn
    from vikingbot.longtask.tools import JournaledToolRegistry, SubmitLongTaskTurnTool

    service = _service(tmp_path)
    service.store = HostStore(service.root / "host.db")
    task = await service.create(_context(), "goal", "req")
    turn = Turn(service, task["task_id"], "turn")
    registry = JournaledToolRegistry(service.config, turn)
    registry.register(SubmitLongTaskTurnTool(turn))
    result = await registry.execute_detailed(
        "submit_long_task_turn",
        {
            "summary": "done",
            "evidence": "claimed",
            "goal_complete": True,
            "validation_operation_id": "made-up",
        },
        session_key=_context().session_key,
    )
    assert str(result.result).startswith("Error:")
    assert turn.proposal is None
    await service.close()


@pytest.mark.asyncio
async def test_background_turn_saves_session_and_syncs_openviking(tmp_path):
    from vikingbot.agent.loop import AgentLoop
    from vikingbot.session.manager import SessionManager

    loop = object.__new__(AgentLoop)
    ov_client = SimpleNamespace(close=AsyncMock())
    loop._ov_clients = {"task": ov_client}
    loop.sessions = SessionManager(tmp_path)
    loop.context = SimpleNamespace(
        build_messages=AsyncMock(return_value=[{"role": "system", "content": "Bot"}])
    )
    loop._build_prompt_history = AsyncMock(return_value=[])
    loop._run_agent_loop = AsyncMock(return_value=("done", None, [], {"total_tokens": 7}, 1))
    loop._ov_session_context_enabled = lambda: True
    loop._submit_openviking_session_and_clear_if_committed = AsyncMock(return_value=True)
    key = LongTaskService._session_key("lt_test")
    await loop.run_background_turn(
        session_key=key,
        prompt="goal",
        instructions="scope",
        tool_registry=ToolRegistry(),
        before_model=AsyncMock(),
    )
    session = loop.sessions.get_or_create(key)
    assert session.messages[-1]["content"] == "done"
    loop._submit_openviking_session_and_clear_if_committed.assert_awaited_once_with(session)
    ov_client.close.assert_awaited_once()
    assert not loop._ov_clients
    kwargs = loop._run_agent_loop.await_args.kwargs
    assert kwargs["serial_tools"] and not kwargs["allow_final_fallback"]
    assert kwargs["publish_events"] is False
    assert kwargs["stop_tool_names"] == ["submit_long_task_turn"]


@pytest.mark.asyncio
async def test_total_round_budget_does_not_reset(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.store = HostStore(service.root / "host.db")
    task = await service.create(_context(), "goal", "req")
    task_id = task["task_id"]
    service.store.update(
        task_id, initialized=1, reason="ready", rounds=service.config.longtask.max_rounds
    )
    decision = {"should_run": True, "effective_action": "normal_run"}
    monkeypatch.setattr(
        service, "_client", lambda _: SimpleNamespace(decision=AsyncMock(return_value=decision))
    )
    with pytest.raises(RuntimeError, match="total round budget"):
        await service._tick(task_id)
    assert service.store.get(task_id)["rounds"] == service.config.longtask.max_rounds
    await service.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("VIKINGBOT_LOOPX_TEST") != "1", reason="Requires LoopX 1.0.5 and qualified Node"
)
async def test_real_loopx_two_round_delivery_and_completion(tmp_path):
    service = _service(tmp_path)
    assert not (service.root / "skills").exists()
    await service.initialize()
    assert set(service.workflows) == set(WORKFLOW_IDS)
    task = await service.create(_context(), "Create two text artifacts and verify both", "req")
    task_id = task["task_id"]
    calls = 0

    async def execute(turn, row, decision):
        nonlocal calls
        calls += 1
        workspace = service._client(task_id).project
        artifact = workspace / f"artifact{calls}.txt"
        artifact.write_text("verified")
        assert artifact.read_text() == "verified"
        if calls == 2:
            assert (workspace / "artifact1.txt").read_text() == "verified"
        operation = service.store.begin_operation(
            task_id, turn.turn_id, "tool", {"path": str(artifact)}
        )
        service.store.end_operation(operation, {"verified": True})
        turn.proposal = {
            "summary": "Both artifacts verified" if calls == 2 else "First artifact verified",
            "evidence": "Exact file contents verified by readback",
            "validation_operation_id": operation,
            "goal_complete": calls == 2,
        }
        if calls == 1:
            turn.proposal["next_todo"] = (
                "Create artifact2.txt and verify both artifacts contain verified"
            )
        return {"text": "", "usage": {}, "iterations": 1}

    service._execute = execute
    try:
        for tick in range(4):
            await service._tick(task_id)
            if tick == 0:
                await service.control(_context(), task_id, "pause")
                assert not service.store.get(task_id)["authorized"]
                await service.control(
                    _context(), task_id, "resume", "Continue with the second artifact"
                )
                assert service.store.get(task_id)["authorized"]
            if service.store.get(task_id)["terminal"]:
                break
        row = service.store.get(task_id)
        assert row["terminal"], json.loads(row["last_decision"])
        assert calls == 2
        assert row["rounds"] == 2
        assert not service.store.unresolved(task_id)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pause_before_initialization_can_resume_without_cli(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.store = HostStore(service.root / "host.db")
    task_id = (await service.create(_context(), "goal", "req"))["task_id"]
    client_call = AsyncMock(side_effect=AssertionError("No LoopX goal exists yet"))
    monkeypatch.setattr(service, "_client", lambda _: SimpleNamespace(call=client_call))
    await service.control(_context(), task_id, "pause")
    assert not service.store.get(task_id)["authorized"]
    await service.control(_context(), task_id, "resume")
    assert service.store.get(task_id)["authorized"]
    client_call.assert_not_called()
    await service.control(_context(), task_id, "cancel")
    with pytest.raises(ValueError, match="cancelled"):
        await service.control(_context(), task_id, "pause")
    assert service.store.get(task_id)["cancelled"]
    await service.close()


@pytest.mark.asyncio
async def test_interrupted_creation_cannot_replay_bootstrap(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.store = HostStore(service.root / "host.db")
    task_id = (await service.create(_context(), "goal", "req"))["task_id"]
    service.store.update(task_id, inflight="creation-turn")
    service.store.recover()
    with pytest.raises(ValueError, match="Creation was interrupted"):
        await service.control(_context(), task_id, "resume")
    assert not service.store.get(task_id)["authorized"]
    assert service.store.get(task_id)["inflight"] == "creation-turn"
    await service.close()


@pytest.mark.asyncio
async def test_failed_write_remains_unresolved_and_cannot_be_resumed(tmp_path):
    from vikingbot.agent.tools.base import Tool
    from vikingbot.longtask.runner import Turn
    from vikingbot.longtask.tools import JournaledToolRegistry

    class FailedWrite(Tool):
        name = "write_file"
        description = "Simulate an I/O failure"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, tool_context):
            return "Error writing file: I/O failure"

    service = _service(tmp_path)
    service.store = HostStore(service.root / "host.db")
    task_id = (await service.create(_context(), "goal", "req"))["task_id"]
    service.store.update(task_id, initialized=1)
    turn = Turn(service, task_id, "turn")
    registry = JournaledToolRegistry(service.config, turn)
    registry.register(FailedWrite())
    with pytest.raises(RuntimeError, match="inspect operation"):
        await registry.execute_detailed("write_file", {}, session_key=_context().session_key)
    assert service.store.unresolved(task_id)
    assert not turn.successful_tools
    with pytest.raises(ValueError, match="reconciliation"):
        await service.control(_context(), task_id, "resume")
    await service.close()


@pytest.mark.asyncio
async def test_invalid_tool_arguments_do_not_create_unknown_side_effects(tmp_path):
    from vikingbot.agent.tools.filesystem import WriteFileTool
    from vikingbot.longtask.runner import Turn
    from vikingbot.longtask.tools import JournaledToolRegistry

    service = _service(tmp_path)
    service.store = HostStore(service.root / "host.db")
    task_id = (await service.create(_context(), "goal", "req"))["task_id"]
    registry = JournaledToolRegistry(service.config, Turn(service, task_id, "turn"))
    registry.register(WriteFileTool())
    result = await registry.execute_detailed("write_file", {}, session_key=_context().session_key)
    assert not result.success and not result.execution_started
    assert not service.store.unresolved(task_id)
    await service.close()


@pytest.mark.asyncio
async def test_quiet_decision_does_not_call_model_or_spend(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.store = HostStore(service.root / "host.db")
    task_id = (await service.create(_context(), "goal", "req"))["task_id"]
    service.store.update(task_id, initialized=1)
    client = SimpleNamespace(
        decision=AsyncMock(
            return_value={
                "should_run": False,
                "effective_action": "quiet",
                "reason": "waiting",
                "scheduler_hint": {
                    "cold_path_detail": {"local_scheduler": {"recommended_interval_minutes": 3}}
                },
            }
        ),
        call=AsyncMock(),
    )
    monkeypatch.setattr(service, "_client", lambda _: client)
    service._execute = AsyncMock()
    await service._tick(task_id)
    service._execute.assert_not_called()
    client.call.assert_not_called()
    assert service.store.get(task_id)["rounds"] == 0
    assert service.store.get(task_id)["next_wake"] > 0
    await service.close()


@pytest.mark.asyncio
async def test_serial_loop_rejects_mixed_submission_batch(tmp_path, monkeypatch):
    from vikingbot.agent.loop import AgentLoop
    from vikingbot.agent.tools.base import Tool
    from vikingbot.hooks.manager import hook_manager
    from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    calls = []

    class RecordTool(Tool):
        name = "record"
        description = "Record execution order"
        parameters = {"type": "object", "properties": {"value": {"type": "string"}}}

        async def execute(self, tool_context, value):
            calls.append(value)
            return value

    class FinishTool(RecordTool):
        name = "finish"

    def request(name, value):
        return ToolCallRequest(id=value, name=name, arguments={"value": value}, tokens=0)

    class Provider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.batches = iter(
                [
                    [request("record", "must-not-run"), request("finish", "mixed")],
                    [request("record", "a"), request("record", "b")],
                    [request("finish", "done")],
                ]
            )

        async def chat(self, **kwargs):
            return LLMResponse(content=None, tool_calls=next(self.batches))

        def get_default_model(self):
            return "test-model"

    async def no_hooks(**kwargs):
        return kwargs

    monkeypatch.setattr(hook_manager, "execute_hooks", no_hooks)
    config = Config(storage_workspace=str(tmp_path))
    loop = AgentLoop(bus=MessageBus(), provider=Provider(), workspace=tmp_path, config=config)
    registry = ToolRegistry(config)
    registry.register(RecordTool())
    registry.register(FinishTool())
    result = await loop._run_agent_loop(
        messages=[{"role": "user", "content": "work"}],
        session_key=_context().session_key,
        publish_events=False,
        tool_registry=registry,
        serial_tools=True,
        stop_tool_names=["finish"],
        allow_final_fallback=False,
        inject_write_experience=False,
    )
    assert calls == ["a", "b", "done"]
    assert result[-1] == 3


@pytest.mark.asyncio
async def test_worker_executes_real_file_tools_with_task_scoped_session(tmp_path, monkeypatch):
    from vikingbot.agent.loop import AgentLoop
    from vikingbot.agent.tools.base import Tool
    from vikingbot.hooks.manager import hook_manager
    from vikingbot.longtask.runner import Turn
    from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

    class MainOnlyTool(Tool):
        name = "mcp_main_only"
        description = "A main-agent tool that must not be inherited by workers"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, tool_context):
            raise AssertionError("Worker must not execute the main agent's MCP tool")

    class Provider(LLMProvider):
        calls = 0

        async def chat(self, messages, tools, **kwargs):
            self.calls += 1
            names = {item["function"]["name"] for item in tools}
            assert {"write_file", "read_file", "submit_long_task_turn"} <= names
            assert not {"spawn", "cron", "message", "start_long_task", "long_task"} & names
            assert not any(name.startswith("mcp_") for name in names)
            assert "Original objective" in str(messages)
            assert "entry workflow" in messages[0]["content"]
            if self.calls == 1:
                name, arguments = "write_file", {"path": "result.txt", "content": "verified"}
            elif self.calls == 2:
                name, arguments = "read_file", {"path": "result.txt"}
            else:
                readback = next(item for item in reversed(messages) if item["role"] == "tool")
                assert "verified" in readback["content"]
                operation = readback["content"].splitlines()[0].removeprefix("operation_id=")
                name = "submit_long_task_turn"
                arguments = {
                    "summary": "File verified",
                    "evidence": "Read back result.txt and checked exact contents",
                    "validation_operation_id": operation,
                    "goal_complete": True,
                }
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id=str(self.calls), name=name, arguments=arguments, tokens=0)
                ],
                usage={"total_tokens": 1},
            )

        def get_default_model(self):
            return "test-model"

    async def no_hooks(**kwargs):
        return kwargs

    monkeypatch.setattr(hook_manager, "execute_hooks", no_hooks)
    connect_mcp = AsyncMock(side_effect=AssertionError("Worker must not connect to MCP"))
    monkeypatch.setattr(AgentLoop, "_connect_mcp", connect_mcp)
    config = Config(storage_workspace=str(tmp_path))
    config.agents.session_context_enabled = False
    config.agents.subagent_enabled = True
    config.tools.cron.enabled = True
    agent = AgentLoop(
        bus=MessageBus(),
        provider=Provider(),
        workspace=tmp_path,
        config=config,
        mcp_servers={"main_only": {"command": "must-not-start"}},
    )
    agent.tools.register(MainOnlyTool())
    service = LongTaskService(agent)
    service.store = HostStore(service.root / "host.db")
    service.workflows = {"loopx": "entry workflow", "loopx-project": "project workflow"}
    task_id = (await service.create(_context(), "Write and verify result.txt", "req"))["task_id"]
    turn = Turn(service, task_id, "turn")
    try:
        result = await service._execute(turn, service.store.get(task_id), {"should_run": True})
        assert result["usage"]["total_tokens"] == 3
        assert turn.proposal["goal_complete"]
        assert not service.store.unresolved(task_id)
        assert (service._client(task_id).project / "result.txt").read_text() == "verified"
        session = agent.sessions.get_or_create(service._session_key(task_id))
        assert session.messages
        assert not agent.sessions.get_or_create(_context().session_key).messages
        connect_mcp.assert_not_awaited()
        assert agent.tools.has("mcp_main_only")
        assert config.agents.subagent_enabled and config.tools.cron.enabled
    finally:
        await service.close()
        await agent.close_mcp()
