"""Compile excludes old source bodies before preparing model input."""

from unittest.mock import AsyncMock

import pytest
from vikingbot.compile.models import CompileLimits
from vikingbot.compile.service import BotCompileService, _compile_time


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
