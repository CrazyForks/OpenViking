# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Snapshot ``file_paths`` scope against the real binding (#4966).

A deleted experience file committed via ``paths`` took a Tree lock, whose
missing-target resolver materialized ``example.md/`` as a directory and the
deletion was then committed as a noop. ``file_paths`` takes an Exact lock.
"""

import pytest

from openviking.pyagfs import get_binding_client
from openviking.pyagfs.exceptions import AGFSNotFoundError
from openviking.server.identity import RequestContext, Role
from openviking.storage.viking_fs import VikingFS
from openviking_cli.session.user_id import UserIdentifier


@pytest.fixture
def snapshot_env(tmp_path):
    client_type, _ = get_binding_client()
    fs_root = tmp_path / "fs"
    fs_root.mkdir()
    config = tmp_path / "ragfs.toml"
    config.write_text(
        f'[git]\nenabled=true\nbackend="local"\n[git.local]\nbase_dir="{tmp_path / "git"}"\n'
    )
    client = client_type(git_config_path=str(config))
    client.mount("localfs", "/local", {"local_dir": str(fs_root)})
    return VikingFS(agfs=client), fs_root


def _ctx(role):
    return RequestContext(user=UserIdentifier("test", "alice"), role=role)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.ROOT, Role.USER])
@pytest.mark.parametrize("filename", ["example.md", "extensionless"])
async def test_file_paths_deleted_file_stays_absent(snapshot_env, role, filename):
    vfs, fs_root = snapshot_env
    ctx = _ctx(role)
    uri = f"viking://user/alice/memories/experiences/{filename}"
    target = fs_root / f"test/user/alice/memories/experiences/{filename}"

    await vfs.write_file(uri, "original experience", ctx=ctx)
    created = await vfs.commit(message="create", file_paths=[uri], ctx=ctx)
    await vfs.rm(uri, ctx=ctx)
    assert not target.exists()
    entries_after_delete = sorted(p.name for p in target.parent.iterdir())

    deleted = await vfs.commit(message="record deletion", file_paths=[uri], ctx=ctx)
    assert not target.exists(), "snapshot locking recreated the deleted file as a directory"
    assert sorted(p.name for p in target.parent.iterdir()) == entries_after_delete
    assert deleted["result"] == "created"
    assert deleted["commit_oid"] != created["commit_oid"]
    assert await vfs.show(created["commit_oid"], path=uri, ctx=ctx) == b"original experience"
    with pytest.raises(AGFSNotFoundError):
        await vfs.show(deleted["commit_oid"], path=uri, ctx=ctx)
    repeated = await vfs.commit(message="repeat deletion", file_paths=[uri], ctx=ctx)
    assert repeated["result"] == "noop"
    assert not target.exists()

    await vfs.write_file(uri, "replacement", ctx=ctx)
    assert target.is_file()
    assert target.read_text() == "replacement"
