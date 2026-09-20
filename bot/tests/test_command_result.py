"""Execution status is independent of text, truncation, and presentation hooks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.agent.tools.registry import ToolRegistry
from vikingbot.agent.tools.shell import ExecTool
from vikingbot.config.schema import Config, SessionKey
from vikingbot.sandbox.backends.aiosandbox import AioSandboxBackend
from vikingbot.sandbox.backends.direct import DirectBackend
from vikingbot.sandbox.backends.opensandbox import OpenSandboxBackend
from vikingbot.sandbox.backends.srt import SrtBackend
from vikingbot.sandbox.base import CommandResult


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 1, None])
@pytest.mark.parametrize("backend_type", [AioSandboxBackend, OpenSandboxBackend, SrtBackend])
async def test_remote_command_status_is_not_inferred_from_output(backend_type, exit_code):
    backend = object.__new__(backend_type)
    output = "x" * 12000
    if backend_type is AioSandboxBackend:
        backend._client = SimpleNamespace(
            shell=SimpleNamespace(
                exec_command=AsyncMock(
                    return_value=SimpleNamespace(
                        data=SimpleNamespace(output=output, exit_code=exit_code),
                    )
                ),
            )
        )
    elif backend_type is OpenSandboxBackend:
        pytest.importorskip("opensandbox.models.execd")
        backend._sandbox = SimpleNamespace(
            commands=SimpleNamespace(
                run=AsyncMock(
                    return_value=SimpleNamespace(
                        id="execution",
                        error=None,
                        logs=SimpleNamespace(stdout=[SimpleNamespace(text=output)], stderr=[]),
                    )
                ),
                get_command_status=AsyncMock(
                    return_value=SimpleNamespace(
                        running=False,
                        exit_code=exit_code,
                    )
                ),
            )
        )
    else:
        backend._process = object()
        backend._send_message = AsyncMock()
        backend._wait_for_response = AsyncMock(
            return_value={
                "type": "executed",
                "stdout": output,
                "exitCode": exit_code,
            }
        )
    result = await backend.execute_result("command")
    assert "truncated" in result.output
    assert result.exit_code == exit_code
    assert result.success is (exit_code == 0)
    assert await backend.execute("command") == result.output
    if backend_type is OpenSandboxBackend:
        backend._sandbox.commands.get_command_status.assert_awaited_with("execution")


@pytest.mark.asyncio
async def test_opensandbox_execution_error_cannot_be_success():
    pytest.importorskip("opensandbox.models.execd")
    backend = object.__new__(OpenSandboxBackend)
    backend._sandbox = SimpleNamespace(
        commands=SimpleNamespace(
            run=AsyncMock(
                return_value=SimpleNamespace(
                    id="execution",
                    error=SimpleNamespace(value="Execution failed"),
                    logs=None,
                )
            ),
            get_command_status=AsyncMock(return_value=SimpleNamespace(running=False, exit_code=0)),
        )
    )
    assert not (await backend.execute_result("command")).success


@pytest.mark.asyncio
async def test_direct_output_is_not_status(tmp_path):
    backend = DirectBackend(Config().sandbox, "test", tmp_path)
    await backend.start()
    try:
        result = await backend.execute_result("printf 'Error: this is data'; exit 0")
        assert result.success and result.output == "Error: this is data"
        result = await backend.execute_result("exit 7")
        assert not result.success and result.exit_code == 7
        # exec replaces the shell, so timeout leaves no child process behind.
        result = await backend.execute_result("exec sleep 2", timeout=0.01)
        assert not result.success and result.exit_code is None
    finally:
        await backend.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 1, None])
async def test_post_hook_cannot_rewrite_execution_status(monkeypatch, exit_code):
    from vikingbot.hooks.manager import hook_manager

    async def hooks(*, result=None, **kwargs):
        return {"result": "Reformatted output"}

    monkeypatch.setattr(hook_manager, "execute_hooks", hooks)
    sandbox = SimpleNamespace(
        execute_result=AsyncMock(return_value=CommandResult("output", exit_code))
    )
    manager = SimpleNamespace(
        get_sandbox=AsyncMock(return_value=sandbox), to_workspace_id=lambda _: "test"
    )
    registry = ToolRegistry(Config())
    registry.register(ExecTool())
    result = await registry.execute_detailed(
        "exec",
        {"command": "command"},
        session_key=SessionKey(type="test", channel_id="test", chat_id="test"),
        sandbox_manager=manager,
    )
    assert result.success is (exit_code == 0)
    assert result.result == "Reformatted output"
