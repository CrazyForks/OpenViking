"""Prepare accepted artifacts, Wiki navigation and revision-bound publication operations."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from openviking.core.namespace import relative_uri_path
from openviking.utils.path_safety import safe_join_viking_uri
from openviking_cli.exceptions import OpenVikingError
from vikingbot.compile import file_ops
from vikingbot.compile.plan import content_hash
from vikingbot.compile.renderer import (
    RenderedBundle,
    _link_uri,
    finalize_resource_output,
    relocate_wiki_links,
    validate_relative_file_path,
)

if TYPE_CHECKING:
    from vikingbot.compile.pipeline import Pipeline


async def run(runtime: Pipeline, references: list[str], *, partial=False) -> RenderedBundle:
    """Validate one writer per path and prepare revision-bound publication operations.

    References name accepted task artifacts; the returned bundle contains write
    operations for the service to publish, without performing target writes here.
    Navigation uses code-rendered direct-child links and reads only ancestor indexes.
    Retained arbitrary pages are never enumerated or loaded; old entries are preserved.
    Partial recovery retains the same write guards. Resource recovery permits missing
    prescribed outputs; Skill packages require all declared files before publication.
    Navigation does not invoke models or Skill scripts, including during recovery.
    """
    files, owners, revisions, source_uris, origins, outputs = {}, {}, {}, {}, {}, {}
    relocations = {}
    for reference in references:
        artifact = await runtime.files.get(reference)
        if content_hash(artifact["content"]) != artifact["sha256"]:
            raise ValueError("Prepared artifact content hash mismatch")
        path = validate_relative_file_path(artifact["path"])
        if path in owners:
            raise ValueError(f"Multiple output writers claim {path}")
        owners[path] = artifact["owner"]
        files[path] = artifact["content"].encode()
        revisions[path] = artifact["base_hash"]
        origin = artifact.get("origin", path)
        origins[path] = origin
        relocations[origin] = path if origin not in relocations else None
        if any(ref not in runtime.evidence for ref in artifact["source_refs"]):
            raise ValueError("Artifact refers to missing source evidence")
        source_uris[path] = sorted(
            {runtime.evidence[ref]["uri"] for ref in artifact["source_refs"]}
        )
        outputs[path] = {"source_refs": artifact["source_refs"]}
    existing = {}
    wiki_files = {
        path: payload for path, payload in files.items() if file_ops.is_wiki(runtime, path, payload)
    }
    if wiki_files:
        for path, payload in wiki_files.items():
            wiki_files[path] = relocate_wiki_links(
                payload.decode(),
                origin=origins[path],
                path=path,
                target_uri=runtime.target,
                relocations=relocations,
                known_paths=set(files),
            ).encode()
        indexes: set[str] = set()
        for path in wiki_files:
            parts = path.split("/")[:-1]
            indexes.update("/".join([*parts[:i], "index.md"]) for i in range(len(parts) + 1))
        for path in sorted(indexes - set(files)):
            text = await file_ops.load_old(runtime, path)
            if text is not None:
                existing[path] = text.encode()
                revisions[path] = content_hash(text)
        finalized = finalize_resource_output(
            wiki_files,
            target_uri=runtime.target,
            source_roots={key: info["uri"] for key, info in runtime.evidence.items()},
            existing_files=existing,
            known_paths=set(files) | set(existing),
            partial_catalog=True,
            source_uris_by_path=source_uris,
        )
        missing = set()
        for entry in finalized.link_report.get("unresolved", []):
            source = safe_join_viking_uri(runtime.target, entry["source_path"])
            uri = _link_uri(re.split(r"[#?]", entry["target"], maxsplit=1)[0], source)
            path = relative_uri_path(runtime.target, uri)
            if not path or path in missing:
                continue
            try:
                await runtime.client.stat(safe_join_viking_uri(runtime.target, path))
            except OpenVikingError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                missing.add(path)
        if missing:
            # A missing exact target and unique matching title allow a local repair;
            # unrecalled historical pages never get redirected by basename alone.
            finalized = finalize_resource_output(
                wiki_files,
                target_uri=runtime.target,
                source_roots={key: info["uri"] for key, info in runtime.evidence.items()},
                existing_files=existing,
                known_paths=set(files) | set(existing),
                partial_catalog=True,
                source_uris_by_path=source_uris,
                verified_missing_paths=missing,
            )
            broken = [
                entry
                for entry in finalized.link_report.get("unresolved", [])
                if relative_uri_path(
                    runtime.target,
                    _link_uri(
                        re.split(r"[#?]", entry["target"], maxsplit=1)[0],
                        safe_join_viking_uri(runtime.target, entry["source_path"]),
                    ),
                )
                in missing
            ]
            if broken:
                runtime.warnings.append(
                    f"Unresolved links to {len(broken)} verified missing targets."
                )
        files.update(finalized.files)
    else:
        finalized = None
    if set(runtime.contract.required_paths) - set(files):
        if not partial or runtime.skill_target:
            raise ValueError("Missing contract-required output paths")
        runtime.warnings.append("Partial output is missing contract-required paths.")
    rendered = RenderedBundle()
    for path, payload in files.items():
        owners.setdefault(path, "runtime:navigation")
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError("Final output including navigation/citations exceeds 8 MiB")
        uri = safe_join_viking_uri(runtime.target, path)
        old = runtime.old.get(path)
        revision = revisions.get(path)
        if revision is None and runtime.skill_target:
            # Detect collisions even when a plan deliberately skips history matching.
            if await file_ops.load_old(runtime, path) is not None:
                raise ValueError(f"Create conflicts with an existing target: {uri}")
        elif revision is not None and (old is None or content_hash(old) != revision):
            raise ValueError(f"Stale prepared artifact: {uri}")
        # Resource writes recheck even identical cached bytes under server locks;
        # concurrent changes become per-file conflicts in the publication result.
        if runtime.skill_target and old is not None and payload == old.encode():
            rendered.unchanged.append(uri)
            continue
        operation = {
            "uri": uri,
            "content": payload.decode(),
            "mode": "replace" if revision else "create",
        }
        if revision:
            operation["expected_sha256"] = revision
        rendered.operations.append(operation)
        (rendered.updated if revision else rendered.created).append(uri)
        if file_ops.is_wiki(runtime, path, payload):
            rendered.wiki_uris.append(uri)
    if finalized:
        rendered.link_count, rendered.link_report = finalized.link_count, finalized.link_report
        unresolved = rendered.link_report.get("unresolved", [])
        rendered.link_report["unresolved_count"] = len(unresolved)
        rendered.link_report["unresolved"] = unresolved[:20]
    for path, output in outputs.items():
        output["sha256"] = content_hash(files[path])
    await runtime.files.put(
        "merge", {"owners": owners, "prepared_files": len(files), "outputs": outputs}
    )
    return rendered
