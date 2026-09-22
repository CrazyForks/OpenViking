# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.storage.expr import Eq, Or, PathScope
from openviking.storage.viking_fs import VikingFS
from openviking_cli.session.user_id import UserIdentifier


def _ctx() -> RequestContext:
    return RequestContext(
        user=UserIdentifier(account_id="acct", user_id="alice"),
        role=Role.ROOT,
    )


@pytest.mark.asyncio
async def test_strict_recursive_delete_clears_orphan_vector_subtree_when_source_is_missing(
    monkeypatch,
):
    session_uri = "viking://user/alice/sessions/session-1"
    session_path = "/local/acct/user/alice/sessions/session-1"
    agfs = SimpleNamespace(stat=AsyncMock(side_effect=FileNotFoundError(session_path)))
    vector_store = SimpleNamespace(
        delete_uris=AsyncMock(),
        delete_uri_scope=AsyncMock(),
        count=AsyncMock(return_value=0),
    )
    fs = VikingFS.__new__(VikingFS)
    fs._async_agfs = agfs
    fs.vector_store = vector_store
    fs.acl_manager = None
    fs._deletion_guard = None
    fs._bound_ctx = SimpleNamespace(get=lambda: None)
    monkeypatch.setattr(fs, "_ensure_access", AsyncMock())
    monkeypatch.setattr(fs, "_uri_to_path", lambda uri, ctx=None: session_path)
    monkeypatch.setattr(fs, "_path_to_uri", lambda path, ctx=None: session_uri)
    monkeypatch.setattr(fs, "_collect_uris", AsyncMock(return_value=[]))
    monkeypatch.setattr(fs, "_confirm_fs_scope_cleared", AsyncMock())

    await fs.rm(session_uri, recursive=True, ctx=_ctx(), strict=True)

    vector_store.delete_uris.assert_awaited_once()
    vector_store.delete_uri_scope.assert_awaited_once_with(
        _ctx(),
        session_uri,
    )
    assert vector_store.count.await_count == 2
    assert vector_store.count.await_args_list[-1].kwargs == {
        "filter": Or([Eq("uri", session_uri), PathScope("uri", session_uri, depth=-1)]),
        "ctx": _ctx(),
    }
