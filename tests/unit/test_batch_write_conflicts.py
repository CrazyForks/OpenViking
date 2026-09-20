"""Target-state conflicts preserve concurrent edits without blocking compatible writes."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.storage.content_write import ContentWriteCoordinator
from openviking_cli.exceptions import ConflictError, PermissionDeniedError


def coordinator_for(files):
    """Model locked storage; every target read/write must hold the publication lease."""
    locked = False

    async def acquire(paths):
        nonlocal locked
        locked = True
        return "lease"

    async def release(lease):
        nonlocal locked
        locked = False

    async def stat(uri, **kwargs):
        assert locked
        return {"not_found": uri not in files, "isDir": files.get(uri) == "directory"}

    async def read(uri, **kwargs):
        assert locked
        return files[uri]

    async def write(uri, content, **kwargs):
        assert locked
        files[uri] = content.encode() if isinstance(content, str) else content

    fs = SimpleNamespace(
        _async_agfs=SimpleNamespace(
            pathlock_acquire_exact_batch=AsyncMock(side_effect=acquire),
            pathlock_release=AsyncMock(side_effect=release),
        ),
        _uri_to_path=lambda uri, **kwargs: uri,
        _ensure_access=AsyncMock(),
        read_file_bytes=AsyncMock(side_effect=read),
    )
    coordinator = ContentWriteCoordinator(fs)
    coordinator._validate_batch_root = AsyncMock()
    coordinator._safe_stat = AsyncMock(side_effect=stat)
    coordinator._write_in_place = AsyncMock(side_effect=write)
    coordinator._refresh_batch = AsyncMock(return_value=None)
    return coordinator


@pytest.mark.parametrize("skip", [False, True])
async def test_changed_target_preserved_and_compatible_files_follow_conflict_policy(skip):
    root = "viking://resources/out"
    changed, good = root + "/b.md", root + "/a.md"
    safe_update = root + "/c.md"
    files = {changed: b"Concurrent edit", safe_update: b"Original"}
    coordinator = coordinator_for(files)
    operations = [
        {"uri": good, "mode": "create", "content": "Published"},
        {
            "uri": changed,
            "mode": "replace",
            "content": "Original",
            "expected_sha256": hashlib.sha256(b"Original").hexdigest(),
        },
        {
            "uri": safe_update,
            "mode": "replace",
            "content": "Updated",
            "expected_sha256": hashlib.sha256(b"Original").hexdigest(),
        },
    ]
    if skip:
        result = await coordinator.batch_write(
            root_uri=root, operations=operations, ctx=None, wait=False, skip_conflicts=True
        )
        assert files[good] == b"Published"
        assert result["created"] == [good]
        assert result["updated"] == [safe_update]
        assert files[safe_update] == b"Updated"
        assert [c["uri"] for c in result["conflicts"]] == [changed]
        assert result["conflicts"][0]["code"] == "CONFLICT"
        assert coordinator._refresh_batch.call_args.kwargs["refresh_kinds"] == {
            good: "added",
            safe_update: "modified",
        }
    else:
        with pytest.raises(ConflictError):
            await coordinator.batch_write(root_uri=root, operations=operations, ctx=None)
        coordinator._write_in_place.assert_not_awaited()
    assert files[changed] == b"Concurrent edit"
    coordinator._viking_fs._async_agfs.pathlock_release.assert_awaited_once()


async def test_all_target_conflicts_and_unchanged_bytes_are_reported_without_writes():
    root = "viking://resources/out"
    files = {
        root + "/created.md": b"Other writer",
        root + "/same.md": b"Same",
        root + "/directory.md": "directory",
    }
    coordinator = coordinator_for(files)
    operations = [
        {"uri": root + "/created.md", "mode": "create", "content": "Draft"},
        {
            "uri": root + "/deleted.md",
            "mode": "replace",
            "content": "Draft",
            "expected_sha256": hashlib.sha256(b"Deleted").hexdigest(),
        },
        {"uri": root + "/directory.md", "mode": "create", "content": "Draft"},
        {
            "uri": root + "/same.md",
            "mode": "replace",
            "content": "Same",
            "expected_sha256": hashlib.sha256(b"Same").hexdigest(),
        },
    ]
    result = await coordinator.batch_write(
        root_uri=root, operations=operations, ctx=None, skip_conflicts=True
    )
    assert len(result["conflicts"]) == 3
    assert result["created"] == result["updated"] == []
    assert result["unchanged"] == [root + "/same.md"]
    coordinator._write_in_place.assert_not_awaited()
    coordinator._refresh_batch.assert_not_awaited()


async def test_skip_conflicts_does_not_suppress_permission_errors():
    coordinator = coordinator_for({})
    coordinator._viking_fs._ensure_access.side_effect = PermissionDeniedError("denied")
    with pytest.raises(PermissionDeniedError):
        await coordinator.batch_write(
            root_uri="viking://resources/out",
            operations=[{"uri": "viking://resources/out/a.md", "mode": "create", "content": "x"}],
            ctx=None,
            skip_conflicts=True,
        )
    coordinator._write_in_place.assert_not_awaited()
