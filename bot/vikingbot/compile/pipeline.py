"""Finite Map/Shuffle/Reduce/Merge execution inside an existing Compile task."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import asdict
from functools import partial
from typing import Any

import yaml

from openviking.core.namespace import classify_uri, relative_uri_path
from openviking.core.skill_loader import validate_skill_format
from openviking.utils.path_safety import safe_join_viking_uri
from openviking.utils.skill_processor import validate_skill_name
from openviking_cli.exceptions import OpenVikingError
from vikingbot.compile.models import CompileFailure, utc_now
from vikingbot.compile.pipeline_io import JsonModel, ModelCallError, TaskFiles, bounded_jobs
from vikingbot.compile.plan import (
    Contract,
    FileDraft,
    FileResponse,
    Group,
    InputReferenceError,
    PlanProposal,
    Record,
    RecordResponse,
    StrictModel,
    content_hash,
    digest,
    parse_plan,
)
from vikingbot.compile.renderer import (
    RenderedBundle,
    _link_uri,
    _split_frontmatter,
    finalize_resource_output,
    relocate_wiki_links,
    validate_relative_file_path,
    validate_resource_file,
)
from vikingbot.compile.shuffle import Shuffle
from vikingbot.compile.skill_resources import SkillResources

_PLANNER = """Plan this collection using the full authoritative Skill, attachments and request.
Return contract.extract (what to extract), routing (what needs joint processing), distinguish (scope field names and meanings),
and reduce (how to synthesize). Keep instructions specific to each operator; reference Skill rules
instead of copying them. Do not enumerate inputs or jobs. Samples are excerpts, not complete sources.
Omit plan for the default map(extract) -> shuffle(routing, against=target) -> reduce -> merge flow.
Transforms default to direct execution; extract produces records and reduce produces final files.
Choose agent for iterative work, Skill scripts or large/multiple files requiring scratch references.
Declare only sufficient semantic payload fields for records; file transforms need no fields.
Prefer fields as a mapping from each field name to one simple sentence describing it; descriptions are optional.
Independent ready files carry complete facts once in ready_content/ref, with routing and relations
in payload. Permit null evidence fields when their facts are already in the ready body; never require
both full evidence and finished text. Fragments requiring joint synthesis retain sufficient evidence
in payload and leave ready content absent. Preserve all conditions, exceptions and ambiguity.
Runtime supplies source IDs, ranges, hashes and deduplicated counts; do not regenerate them in fields.
Express distinguish as a mapping from short field names to their meanings, for example
{"subject": "The product or entity and its applicability", "version": "The stated effective version"}.
Keep each rule or extraction explanation in the value, so Map understands what each field means.
Scope preserves applicability and identity evidence, not exact-match grouping keys. Paths are hints.
Shuffle gathers potentially related evidence; Reduce decides whether to combine or separate facts,
how many files to produce and their paths. One group may contain different scopes and output files.
Use contract.options only for additional settings: preserve/validation for requirements beyond the
Skill, required_paths for prescribed outputs, output_format=wiki for OKF pages, and unsupported for
unmet capabilities. Unsupported requirements must be reported, never silently weakened.
For necessary hierarchical aggregation, declare options.combine with sufficient records and
options.overflow=structured. Without aggregation, Reduce receives the complete joint input.
For multiple synthesis levels, supply options.synthesize/final_routing and a custom plan. Options
become direct contract attributes in the DSL. An intermediate reduce explicitly sets output=records;
the final transform sets output=files. All Map transforms produce records.
Custom plans use the default plan's assignment syntax and bound sources, target, contract, p.
Map accepts sources/records, shuffle accepts records, reduce accepts groups; omit against for
intermediate grouping. Consume each dataset once and finish with one merge; at most 12 nodes,
no imports, loops or arbitrary calls. Overflow uses the contract.
Runtime handles scoped history recall, source rereads, provenance, ownership and conditional writes.
Full Skill attachments are supplied; read missing referenced resources with read_skill_resource.
Agent tools read/write/edit private scratch files and run only Skill-supplied Python scripts through
run_skill_script. There are no global history scans, external network tools or scratch script execution.
Model/time are supplied. Runtime builds OKF navigation: never route content to OKF index.md or require
derived indexes. Ordinary files, including non-Wiki index.md, follow the Skill.
"""

_FIDELITY = """Do not add plausible business consequences, instructions or definitions that the
sources do not establish. Do not turn examples into rules. Retain ambiguity in the actor of a
condition, conjunctions, slash notation and missing units; quote an unclear clause instead of
selecting a plausible interpretation or inventing an obligation.
"""

_SKILL_OUTPUT = """The target is a Skill namespace. Produce one complete Skill package with
all files under the same <skill-name>/ directory and a valid <skill-name>/SKILL.md.
Declare output_format=files and put that SKILL.md path in contract.options.required_paths.
Choose the package name once in the contract; all transforms use that directory.
SKILL.md requires YAML name matching its directory and a nonempty description. Preserve
attachments in their native formats. No Wiki frontmatter, citations or navigation is added
by the runtime. Use hierarchical synthesis when the Skill requires a shared entry point;
individual work sets can generate different package files.
"""

_RECORDS = (
    _FIDELITY
    + """Transform supplied inputs into the declared record fields, preserving the Skill.
Use record_fields names and descriptions as a guide for payload facts, which may contain structured JSON.
Use short scope keys with evidence-based values; scope_fields explains each suggested field.
Omit unavailable fields and add useful fields when the evidence calls for them.
Finished content and its path always use top-level ready_content/ready_path.
Each record.inputs lists ONLY supplied input IDs supporting its payload; runtime assigns IDs,
stores complete source evidence and propagates provenance. Every input must occur in a record
or an explicit exclusion, never both. References establish provenance, not semantic completeness.
Read all supplied text; preserve required detail, citations, exceptions and applicability conditions.
Use short routing_text; never group just by title. Independent finished files use ready_content with
ready_path and concise identity/scope/relationship payloads. Evidence details already in the body
need not be repeated in payload. Check finished content against originals and every Skill rule.
Fragments requiring joint synthesis retain full necessary evidence in payload and no ready content.
Other files generated in this assignment can be referenced by their exact titles; runtime resolves
unambiguous mentions after ownership and final paths are accepted. Never assume proposed paths exist.
Determine boundaries and relations before writing. Each page has one primary Skill classification
and every required directory level; shared sources/products do not unite different reader purposes.
For OKF Wiki, write content pages only; runtime owns navigation, so never draft or emit OKF index.md.
Ordinary non-Wiki files follow their Skill.
"""
)

_FILES = (
    _FIDELITY
    + """Generate final files for this output group following all Skill requirements.
Submit files[].path with content or content_ref. ready_* keys belong only to record transforms.
Original evidence is authoritative; derived payload fields are fallible extraction notes.
Verify numbers, units, boundaries, exceptions and claimed uncertainty against original_evidence.
original_evidence contains complete original ranges. Consult read_evidence when details are missing,
ambiguous or conflicting; use sufficient supplied evidence directly. Correct extraction mistakes.
Use original source citations carried by evidence; preserve exceptions and applicability conditions.
For existing files, prefer exact unique-anchor patches and return the supplied base_hash.
Patch anchors must be copied from the supplied old text, occur exactly once and not overlap.
Full content replacement is allowed for small files or whole-file changes; do not mix with patches.
For a selected large-file section, patches can only modify that section; never replace other text.
For new files return exact content and no base_hash. Each work set contains candidate evidence,
not a predetermined file: combine supplements, preserve conflicting facts with their applicability,
and split independent subjects into as many files as needed. Input paths and scopes do not bind
output paths. historical_files supplies recalled paths, content and base_hash for comparison;
update relevant files using their exact path and hash, leaving irrelevant history unchanged.
A Skill's overall required outputs are fulfilled collectively across work sets.
related_outputs is a partial catalog of accepted current-task files with real final paths. Use
relevant entries for links and avoid duplicating their full bodies. An unlisted page is unknown,
not absent; never claim another group has not produced a page. When a target path is unknown,
mention the relevant subject naturally without inventing a destination.
related_subjects contains assigned topics, not accepted files. Use their exact names for relevant
cross-page mentions; runtime links them only if an unambiguous matching final page exists.
Set each file.inputs to supplied input IDs actually used by that file. Multiple inputs may support
one file; do not create a separate file for every input. Do not claim unrelated sources.
Wiki files require YAML type, title, single-line description and source citations. Generic files
follow their declared format. Never fabricate requirements, source identities or existing paths.
Runtime exclusively builds OKF navigation indexes after all content files are prepared. Submit
content files only; runtime fulfills any navigation duties mentioned in the Skill or transform.
"""
)


def apply_file(draft: FileDraft, old: str | None) -> str:
    """Assemble a revision-bound file, rejecting ambiguous, overlapping or stale patches.

    Bytes outside patch anchors are preserved exactly, including whitespace and frontmatter.
    """
    if old is None:
        if draft.base_hash is not None or draft.patches or draft.content is None:
            raise ValueError("New files require content without a base revision or patches")
        return draft.content
    if draft.base_hash != content_hash(old):
        raise ValueError("Old target revision does not match the requested change")
    if draft.content is not None:
        if draft.patches:
            raise ValueError("Full replacement cannot be mixed with patches")
        return draft.content
    if not draft.patches:
        raise ValueError("Existing files require content or patches")
    edits = []
    for patch in draft.patches:
        if old.count(patch.old) != 1:
            raise ValueError("Patch anchor must occur exactly once in its permitted old scope")
        start = old.index(patch.old)
        edits.append((start, start + len(patch.old), patch.new))
    edits.sort()
    if any(a[1] > b[0] for a, b in zip(edits, edits[1:], strict=False)):
        raise ValueError("Patch anchors overlap")
    value = old
    for start, end, replacement in reversed(edits):
        value = value[:start] + replacement + value[end:]
    return value


class MergedContent(StrictModel):
    """Complete merged page text; runtime retains its path and publication revision."""

    content: str


class Pipeline:
    """A task-owned finite executor, reusing provider slots, sandbox, client and commit API.

    Payloads/evidence stay in task files; batching targets guide model assignments.
    The lifecycle is owned by BotCompileService; this object provides same-workspace
    retries, not automatic service-crash recovery. Input collections have no fixed
    aggregate size or elapsed-time cutoff; individual calls retain execution timeouts.
    """

    def __init__(
        self, *, client, sandbox, provider, model, temperature, limits, request, skill, usage
    ):
        self.client, self.limits, self.request = client, limits, request
        self.target, self.skill = request.to, skill
        self.skill_target = classify_uri(self.target).context_type == "skill"
        self.skill_name = ""  # One package directory, selected by the validated contract.
        self.files = TaskFiles(sandbox)
        self.metrics: Counter = Counter()
        self.model = JsonModel(
            provider, model, temperature, self.files, limits, usage, self.metrics
        )
        self.resources = SkillResources(client, request.skill, self.files)
        self.model.resources = self.resources
        self.contract: Contract
        self.system = ""
        self.records: dict[str, Record] = {}
        self.evidence: dict[str, dict] = {}
        self.status: dict[str, str] = {}
        self.old: dict[str, str | None] = {}
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.artifacts: list[str] = []  # Only fully accepted final-file submissions.
        self.catalog: dict[str, dict] = {}  # Accepted task outputs, never historical enumeration.
        # Each output path belongs to the group whose file is accepted last.
        self.owners: dict[str, str] = {}

    async def run(self, batches) -> RenderedBundle:
        """Plan, expand and execute collections; errors preserve honest pending/failed states."""
        started = time.monotonic()
        complete = False
        active_stage, stage_start = None, started
        self.records.clear()
        self.evidence.clear()
        self.status.clear()
        self.old.clear()
        self.failures.clear()
        self.warnings.clear()
        self.artifacts.clear()
        self.catalog.clear()
        self.owners.clear()
        try:
            sources = await self.seed(batches)
            runtime = await self.files.get("runtime")
            if runtime is None:
                runtime = {
                    "model": self.model.model,
                    "model_settings": self.model.identity,
                    "time": utc_now(),
                    "skill": self.request.skill,
                }
                await self.files.put("runtime", runtime)
            self.metrics["input_ranges"] = len(sources)
            self.metrics["input_files"] = len({x["uri"] for x in self.evidence.values()})
            samples = []
            for record in sources[:2]:
                source = await self.files.get(record.payload_ref)
                samples.append({"uri": source["uri"], "excerpt": source["text"][:500]})
            references = self.resources.references(self.skill)
            for reference in references:
                await self.resources.read(reference)

            def validate_plan(proposal):
                parse_plan(proposal.plan, proposal.contract)
                if self.skill_target:
                    paths = [
                        p
                        for p in proposal.contract.required_paths
                        if p.count("/") == 1 and p.endswith("/SKILL.md")
                    ]
                    if len(paths) != 1 or proposal.contract.output_format != "files":
                        raise ValueError(
                            "Skill output requires files and one required <skill-name>/SKILL.md"
                        )
                    try:
                        name = validate_skill_name(paths[0].split("/")[0])
                    except OpenVikingError as exc:
                        raise ValueError(str(exc)) from exc
                    if any(not p.startswith(name + "/") for p in proposal.contract.required_paths):
                        raise ValueError("Required Skill outputs must share the package directory")
                missing = set(references) - set(self.resources.hashes)
                if missing:
                    raise ValueError(
                        f"Read the referenced Skill resources before planning: {sorted(missing)}"
                    )

            proposal = await self.model.ask(
                "plan",
                _PLANNER + (_SKILL_OUTPUT if self.skill_target else ""),
                {
                    "skill": self.skill,
                    "skill_resources": references,
                    "runtime": runtime,
                    "request": {
                        "to": self.target,
                        "instruction": self.request.instruction,
                        "source_root_count": len(self.request.from_),
                    },
                    "source_counts": dict(self.metrics),
                    "samples": samples,
                },
                PlanProposal,
                validate_plan,
            )
            self.contract = proposal.contract
            if self.skill_target:
                self.skill_name = next(
                    p.split("/")[0]
                    for p in self.contract.required_paths
                    if p.count("/") == 1 and p.endswith("/SKILL.md")
                )
            nodes = parse_plan(proposal.plan, self.contract)
            self.system = (
                "Original Skill (authoritative):\n"
                + self.skill
                + "\nInstruction:\n"
                + self.request.instruction
                + "\nShared requirements:\n"
                + self.contract.model_dump_json(
                    include={"preserve", "validation", "required_paths"}
                )
                + (_SKILL_OUTPUT if self.skill_target else "")
                + "\nRuntime: "
                + json.dumps(runtime)
                + "\nSkill attachments (read necessary rules before use): "
                + json.dumps(self.resources.hashes)
            )
            contract_hash = digest(
                [self.skill, self.contract.model_dump(), self.model.identity, self.resources.hashes]
            )
            await self.files.put(
                "contract",
                {
                    "hash": contract_hash,
                    "contract": self.contract.model_dump(),
                    "skill": self.skill,
                    "dependencies": dict(self.resources.hashes),
                },
            )
            await self.files.put(
                "plan",
                {
                    "contract_hash": contract_hash,
                    "program": proposal.plan,
                    "nodes": [asdict(n) for n in nodes],
                },
            )
            data: dict[str, Any] = {"sources": sources}
            shuffler = Shuffle(self)
            for node in nodes:
                active_stage = node.name
                stage_start = time.monotonic()
                inputs = data.pop(node.source)
                if node.op == "map":
                    transform = getattr(self.contract, node.task)
                    if transform.execution == "agent":
                        jobs = [[record] for record in inputs]
                    else:
                        jobs = await self.pack(
                            inputs,
                            self.system + _RECORDS + "\nTask: " + transform.instructions,
                            RecordResponse,
                            {
                                "record_fields": transform.fields,
                                "scope_fields": self.contract.distinguish,
                            },
                            max_payload_chars=self.model.reserve,
                        )
                    outputs = await bounded_jobs(
                        enumerate(jobs),
                        partial(self.map_job, node),
                        concurrency=self.limits.source_concurrency,
                        metrics=self.metrics,
                        failures=self.failures,
                    )
                    data[node.name] = [record for output in outputs for record in output]
                elif node.op == "shuffle":
                    data[node.name] = await shuffler.run(node, inputs)
                elif node.op == "reduce":
                    data[node.name] = await self.reduce_sets(node, inputs)
                else:
                    if self.failures:
                        if not inputs:
                            raise ValueError("No accepted final artifacts remain after failed jobs")
                        self.warnings.append(
                            "Partial output; unfinished jobs: " + self.failures[0][:500]
                        )
                    result = await self.merge(inputs, partial=bool(self.failures))
                    complete = not self.failures
                self.metrics[f"{node.name}_milliseconds"] = round(
                    (time.monotonic() - stage_start) * 1000
                )
                active_stage = None
            return result
        except BaseException as exc:
            self.failures.append(str(exc)[:800] or type(exc).__name__)
            if isinstance(exc, Exception):
                raise CompileFailure(
                    "COMPILE_INCOMPLETE", self.failures[-1], stage="pipeline"
                ) from exc
            raise
        finally:
            if active_stage is not None:
                self.metrics[f"{active_stage}_milliseconds"] = round(
                    (time.monotonic() - stage_start) * 1000
                )
            await self.write_coverage()
            self.metrics["total_milliseconds"] = round((time.monotonic() - started) * 1000)
            self.metrics["pending"] = sum(s == "pending" for s in self.status.values())
            self.metrics["unreferenced"] = sum(s == "unreferenced" for s in self.status.values())
            self.metrics["failed"] = sum(s == "failed" for s in self.status.values())
            await self.files.put(
                "summary",
                {
                    "prepared": complete,
                    "committed": False,
                    "metrics": dict(self.metrics),
                    "states": self.status,
                    "errors": self.failures,
                    "warnings": self.warnings,
                },
            )

    async def write_coverage(self, published=()):
        """Record every input's lineage disposition and acknowledged final files with hashes.

        Published URIs come from file/package acknowledgements or verified unchanged
        bytes. A source is delivered only when all its branches finish and all prepared
        supporting files are acknowledged. Unreferenced branches have unconfirmed coverage,
        not an approved exclusion. References alone do not prove semantic quality.
        """
        children = {}
        for record in self.records.values():
            for parent in record.parents:
                children.setdefault(parent, []).append(record.record_id)

        def disposition(states):
            for state in ("failed", "pending", "unreferenced"):
                if state in states:
                    return state
            return "excluded" if all(s == "excluded" for s in states) else "prepared"

        for record in reversed(list(self.records.values())):
            if record.record_id in children and self.status[record.record_id] != "failed":
                self.status[record.record_id] = disposition(
                    [self.status[child] for child in children[record.record_id]]
                )
        manifest = await self.files.get("inputs") or {}
        inputs = {
            uri: {"ranges": {}, "outputs": [], "status": state} for uri, state in manifest.items()
        }
        for reference, evidence in self.evidence.items():
            inputs[evidence["uri"]]["ranges"][reference] = {
                **evidence,
                "status": self.status[reference],
            }
        merged = await self.files.get("merge") or {}
        published = set(published)
        for path, output in merged.get("outputs", {}).items():
            uri = safe_join_viking_uri(self.target, path)
            for source in {self.evidence[ref]["uri"] for ref in output["source_refs"]}:
                inputs[source]["outputs"].append(
                    {
                        "uri": uri,
                        "sha256": output["sha256"],
                        "delivered": uri in published,
                        "source_ranges": [
                            ref
                            for ref in output["source_refs"]
                            if self.evidence[ref]["uri"] == source
                        ],
                    }
                )
        for uri, item in inputs.items():
            if item["ranges"]:
                item["status"] = disposition([r["status"] for r in item["ranges"].values()])
            if (
                item["status"] == "prepared"
                and item["outputs"]
                and all(o["delivered"] for o in item["outputs"])
            ):
                item["status"] = "delivered"
            manifest[uri] = item["status"]
        await self.files.put("inputs", manifest)
        await self.files.put(
            "coverage", {"counts": dict(Counter(manifest.values())), "inputs": inputs}
        )

    async def seed(self, batches) -> list[Record]:
        """Retain exact ranges with content hashes and offsets before any transformation."""
        result = []
        async for batch in batches:
            for part in batch:
                metadata = {
                    "uri": part.uri,
                    "hash": content_hash(part.content),
                    "start_char": part.start_char,
                    "end_char": part.end_char,
                    "start_line": part.start_line,
                    "end_line": part.end_line,
                    "continues_before": part.continues_before,
                    "continues_after": part.continues_after,
                }
                record_id = digest(metadata)[:24]
                if record_id in self.records:
                    continue
                self.evidence[record_id] = metadata
                await self.files.put(
                    f"sources/{record_id}",
                    {**metadata, "text": part.content, "context": part.context},
                )
                record = Record(record_id, f"sources/{record_id}", [record_id], part.uri, {}, [])
                self.register(record)
                result.append(record)
        return result

    def register(self, record: Record) -> None:
        """Track every record's lineage and disposition without truncating the collection."""
        self.records[record.record_id] = record
        self.status.setdefault(record.record_id, "pending")

    async def payload(self, record: Record) -> dict:
        """Resolve one complete assignment payload; evidence files remain addressable."""
        data = await self.files.get(record.payload_ref)
        if data is None:
            raise ValueError(f"Missing evidence/payload: {record.payload_ref}")
        item = {"id": record.record_id, "payload": data}
        if record.ready_ref:
            item["ready_file"] = await self.files.get(record.ready_ref)
        return item

    async def pack(
        self, records, system, schema, extra=None, max_payload_chars=None
    ) -> list[list[Record]]:
        """Split records around soft size targets without rejecting indivisible records.

        Large records remain complete in their own batches. Extraction also uses an
        output-size estimate to avoid packing too many expanding inputs together.
        """
        batches: list[list[Record]] = []
        current: list[Record] = []
        payloads: list[dict] = []
        for record in records:
            payload = await self.payload(record)
            candidate = {"inputs": [*payloads, payload], **(extra or {})}
            # Keep input ID arrays bounded even when source files are tiny.
            if current and (
                len(current) >= 32
                or not self.model.fits(system, candidate, schema)
                or (
                    max_payload_chars is not None
                    and len(json.dumps(candidate["inputs"], ensure_ascii=False)) > max_payload_chars
                )
            ):
                batches.append(current)
                current, payloads = [], []
            current.append(record)
            payloads.append(payload)
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def coverage(inputs, included, excluded) -> None:
        """Validate explicit dispositions; references alone do not assert semantic completeness."""
        expected = {r.record_id for r in inputs}
        exclusions = [e.input for e in excluded]
        if len(set(exclusions)) != len(exclusions) or set(included) & set(exclusions):
            raise InputReferenceError(
                "Inputs cannot be both included and excluded or excluded twice: "
                f"included={included}; excluded={exclusions}"
            )
        accounted = set(included) | set(exclusions)
        if accounted != expected:
            raise InputReferenceError(
                "Every supplied input needs an explicit disposition: "
                f"missing={sorted(expected - accounted)}; unknown={sorted(accounted - expected)}"
            )
        for entry in excluded:
            if entry.duplicate_of and (
                entry.duplicate_of not in set(included) or entry.duplicate_of == entry.input
            ):
                raise InputReferenceError(
                    "Duplicate evidence must name another included input: "
                    f"input={entry.input}; duplicate_of={entry.duplicate_of}; included={included}"
                )

    async def transform(self, label, transform, records, extra=None) -> list[Record]:
        """Produce typed records with deterministic IDs and runtime-propagated evidence sets."""
        system = self.system + _RECORDS + "\nTask: " + transform.instructions
        data = {
            "inputs": [await self.payload(r) for r in records],
            **(extra or {}),
            "record_fields": transform.fields,
            "scope_fields": self.contract.distinguish,
        }

        def validate(response):
            self.coverage(
                records, [i for draft in response.records for i in draft.inputs], response.excluded
            )
            for draft in response.records:
                if draft.ready_content_ref is not None:
                    raise ValueError("Ready references require an agent with scratch access")
                if draft.ready_path is not None:
                    validate_relative_file_path(draft.ready_path)
                if draft.ready_content is not None and draft.ready_path is None:
                    raise ValueError("Finished content requires ready_path")
                if draft.ready_content is not None:
                    ready_group = Group("map", records)
                    self.validate_files(
                        FileResponse(
                            files=[
                                FileDraft(
                                    path=draft.ready_path,
                                    content=draft.ready_content,
                                    inputs=draft.inputs,
                                )
                            ],
                        ),
                        ready_group,
                        [r for r in records if r.record_id in draft.inputs],
                        {},
                    )
                if draft.target_uri and (
                    not relative_uri_path(self.target, draft.target_uri)
                    or draft.target_uri
                    not in json.dumps(
                        [item for item in data["inputs"] if item["id"] in draft.inputs],
                        ensure_ascii=False,
                    )
                ):
                    raise ValueError(
                        "A stable target candidate must occur explicitly in supplied evidence inside to"
                    )

        response = await self.model.ask(
            label, system, data, RecordResponse, validate, agent=transform.execution == "agent"
        )
        outputs = []
        by_id = {r.record_id: r for r in records}
        for index, draft in enumerate(response.records):
            source_refs = sorted(
                {ref for parent in draft.inputs for ref in by_id[parent].source_refs}
            )
            record_id = digest([label, data, index, draft.model_dump(), self.model.identity])[:24]
            payload_ref = f"payloads/{record_id}"
            # Only short source identifiers and runtime counts enter the next prompt.
            evidence_uris = sorted({self.evidence[ref]["uri"] for ref in source_refs})
            await self.files.put(
                payload_ref,
                {
                    "fields": draft.payload,
                    "evidence_ref": f"records/{record_id}",
                    "unique_source_count": len(evidence_uris),
                    "source_examples": evidence_uris[:8],
                    "source_ranges": source_refs,
                    "routing_text": draft.routing_text,
                    "scope": draft.scope,
                },
            )
            record = Record(
                record_id,
                payload_ref,
                source_refs,
                draft.routing_text,
                draft.scope,
                draft.inputs,
                f"ready/{record_id}" if draft.ready_content is not None else None,
                digest(transform.model_dump()),
                draft.target_uri,
            )
            if record.ready_ref:
                await self.files.put(
                    record.ready_ref, {"path": draft.ready_path, "content": draft.ready_content}
                )
            self.register(record)
            await self.files.put(f"records/{record_id}", asdict(record))
            outputs.append(record)
        for exclusion in response.excluded:
            self.status[exclusion.input] = "excluded"
            await self.files.put(f"exclusions/{exclusion.input}", exclusion.model_dump())
        return outputs

    async def job(self, name, inputs, work):
        """Persist one job's outcome; failures never advance source/record completion."""
        entry = {"status": "running", "inputs": [r.record_id for r in inputs]}
        await self.files.put(f"jobs/{name}", entry)
        try:
            try:
                output = await work()
            except OSError:
                self.metrics["job_retries"] += 1
                output = await work()
        except BaseException as exc:
            state = "failed" if isinstance(exc, Exception) else "pending"
            for record in inputs:
                self.status[record.record_id] = state
            await self.files.put(
                f"jobs/{name}", {**entry, "status": state, "error": str(exc)[:800]}
            )
            raise
        await self.files.put(
            f"jobs/{name}", {**entry, "status": "completed", "output_count": len(output)}
        )
        return output

    async def map_job(self, node, item):
        """Expand one packed Map assignment without a parent agent spawning children."""
        index, records = item
        return await self.job(
            f"{node.name}-{index}",
            records,
            lambda: self.transform("map", getattr(self.contract, node.task), records),
        )

    async def load_old(self, path: str) -> str | None:
        """Read a selected target body once in this execution; only NOT_FOUND means absence."""
        path = validate_relative_file_path(path)
        if path not in self.old:
            uri = safe_join_viking_uri(self.target, path)
            try:
                entry = await self.client.stat(uri)
                if entry.get("isDir") or int(entry.get("size") or 0) > 8 * 1024 * 1024:
                    raise ValueError("Selected old target must be a file of at most 8 MiB")
                payload = await self.client.download_bytes(uri)
                if len(payload) > 8 * 1024 * 1024:
                    raise ValueError("Selected old file exceeds 8 MiB")
                self.old[path] = payload.decode("utf-8")
                self.metrics["history_body_reads"] += 1
            except OpenVikingError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                self.old[path] = None
        return self.old[path]

    async def reduce_sets(self, node, groups):
        """Run each group once, then consolidate candidate files sharing a final path.

        Model call failures retain the candidate with the most distinct source URIs.
        Failed groups leave other groups' files available for partial publication.
        """
        outputs = await bounded_jobs(
            groups,
            partial(self.reduce_job, node),
            concurrency=self.limits.merge_concurrency,
            metrics=self.metrics,
            failures=self.failures,
        )
        references = [item for output in outputs for item in output]
        if getattr(self.contract, node.task).output == "records":
            return references
        candidates = {}
        for reference in references:
            artifact = await self.files.get(reference)
            candidates.setdefault(artifact["path"], []).append((reference, artifact))
        return [await self.merge_candidates(items) for items in candidates.values()]

    async def merge_candidates(self, candidates):
        """Return one accepted reference for a path, retaining its main draft on call failure.

        Candidates contain saved references and artifacts. Only successful consolidation
        combines lineage; normal file validation and storage errors propagate.
        """
        candidates.sort(
            key=lambda item: len({self.evidence[r]["uri"] for r in item[1]["source_refs"]}),
            reverse=True,
        )
        reference, main = candidates[0]
        if len(candidates) > 1:
            try:
                result = await self.model.ask(
                    "reduce_merge",
                    self.system + "\nUse the main draft as the basis; integrate the other drafts, "
                    "remove repetition, preserve applicability and sources. Return complete content.",
                    {"main": main, "others": [item[1] for item in candidates[1:]]},
                    MergedContent,
                    None,
                )
            except ModelCallError:
                self.metrics["reduce_merge_fallbacks"] += 1
            else:
                inputs = sorted({i for _, a in candidates for i in a["inputs"]})
                response = FileResponse(
                    files=[
                        FileDraft(
                            path=main["path"],
                            content=result.content,
                            base_hash=main["base_hash"],
                            inputs=inputs,
                        )
                    ],
                )
                old = {main["path"]: self.old[main["path"]]} if main["base_hash"] else {}
                records = [self.records[i] for i in inputs]
                group = Group("merge-" + digest([ref for ref, _ in candidates]), records)
                self.validate_files(response, group, records, old)
                return (
                    await self.save_files(response, group, records, old, origin=main["origin"])
                )[0]
        await self.accept_files([reference])
        return reference

    async def reduce_job(self, node, group: Group):
        """Synthesize a work set into records or any supported collection of files."""
        return await self.job(
            f"{node.name}-{group.group_id}", group.records, lambda: self.reduce_group(node, group)
        )

    async def ready_file(self, group):
        """Reuse only a solitary complete draft with no historical comparison to perform."""
        if group.target_uris or len(group.records) != 1 or not group.records[0].ready_ref:
            return None
        return await self.files.get(group.records[0].ready_ref)

    async def reduce_group(self, node, group: Group):
        """Synthesize a candidate work set, preserving evidence and historical revisions."""
        transform = getattr(self.contract, node.task)
        records, old = group.records, {}
        if transform.output == "files":
            for uri in group.target_uris:
                path = relative_uri_path(self.target, uri)
                if not path:
                    raise ValueError("Historical context is outside the target")
                content = await self.load_old(path)
                if content is None:
                    raise ValueError(f"Recalled historical file no longer exists: {uri}")
                old[path] = content
        ready = await self.ready_file(group) if transform.output == "files" else None
        if ready is not None:
            previous = await self.load_old(ready["path"])
            if previous is not None:
                old[ready["path"]] = previous
            else:
                response = FileResponse(
                    files=[
                        FileDraft(
                            path=ready["path"],
                            content=ready["content"],
                            inputs=[records[0].record_id],
                        )
                    ],
                )
                self.validate_files(response, group, records, old)
                self.metrics["direct_reuse"] += 1
                return await self.save_files(response, group, records, old)
        evidence = sorted({ref for r in records for ref in r.source_refs})
        extra = {
            "group": {"id": group.group_id},
            "scope_fields": self.contract.distinguish,
            "unique_source_count": len({self.evidence[ref]["uri"] for ref in evidence}),
        }
        if transform.output == "files":
            related = [
                entry
                for path, entry in self.catalog.items()
                if set(entry["source_refs"]) & set(evidence)
            ]
            extra["related_outputs"] = [
                {k: v for k, v in entry.items() if k != "source_refs"}
                for entry in sorted(related, key=lambda e: e["path"])[:12]
            ]
            extra["related_subjects"] = [
                {"scope": r.scope, "description": r.routing_text}
                for r in self.records.values()
                if r.schema == records[0].schema
                and r not in records
                and set(r.source_refs) & set(evidence)
            ][:12]
        if old:
            extra["historical_files"] = [
                {"path": path, "content": content, "base_hash": content_hash(content)}
                for path, content in old.items()
            ]
        schema = RecordResponse if transform.output == "records" else FileResponse
        if transform.output == "records":
            extra.update(record_fields=transform.fields, scope_fields=self.contract.distinguish)
        system = (
            self.system
            + (_RECORDS if transform.output == "records" else _FILES)
            + "\nTask: "
            + transform.instructions
        )
        for depth in range(4):
            data = {**extra, "inputs": [await self.payload(r) for r in records]}
            if (
                self.model.fits(system, data, schema)
                or depth == 3
                or self.contract.overflow != "structured"
            ):
                break
            combine = self.contract.combine
            assert combine is not None
            combine_system = self.system + _RECORDS + "\nTask: " + combine.instructions
            chunks = await self.pack(
                records,
                combine_system,
                RecordResponse,
                {"record_fields": combine.fields, "scope_fields": self.contract.distinguish},
            )
            reduced = []
            for chunk in chunks:
                reduced.extend(await self.transform("combine", combine, chunk))
            if not reduced:
                raise ValueError("Overflow aggregation cannot discard all required contributions")
            records = reduced
        if transform.output == "records":
            return await self.transform("reduce", transform, records, extra)
        response = await self.model.ask(
            "reduce",
            system,
            data,
            FileResponse,
            lambda value: self.validate_files(value, group, records, old),
            agent=transform.execution == "agent",
        )
        return await self.save_files(response, group, records, old)

    def validate_files(self, response, group, records, old):
        """Validate supplied lineage, output paths and revisions; unused inputs are allowed."""
        supplied = {record.record_id for record in records}
        paths = [f.path for f in response.files]
        if len(set(paths)) != len(paths):
            raise ValueError("A group cannot submit a path more than once")
        for draft in response.files:
            validate_relative_file_path(draft.path)
            inputs = draft.inputs
            if not inputs or len(inputs) != len(set(inputs)) or not set(inputs) <= supplied:
                raise InputReferenceError(
                    "File inputs must be nonempty, unique supporting supplied record IDs: "
                    f"path={draft.path}; inputs={inputs}; unknown={sorted(set(inputs) - supplied)}"
                )
            current = old.get(draft.path)
            if draft.content_ref is not None:
                raise ValueError(
                    "File references require execution=agent and validated scratch resolution"
                )
            value = apply_file(draft, current)
            if self.skill_target:
                if not draft.path.startswith(self.skill_name + "/"):
                    raise ValueError(f"Skill files must be under {self.skill_name}/")
                if draft.path == f"{self.skill_name}/SKILL.md":
                    validation = validate_skill_format(
                        value, strict=True, skill_dir_name=self.skill_name, source_path=draft.path
                    )
                    if not validation["valid"]:
                        raise ValueError(
                            "; ".join(issue["message"] for issue in validation["errors"])
                        )
            if draft.content_sha256 and content_hash(value) != draft.content_sha256:
                raise ValueError("Artifact content hash mismatch")
            if len(value.encode()) > 8 * 1024 * 1024:
                raise ValueError("Assembled output exceeds 8 MiB")
            wiki = self.is_wiki(draft.path, value.encode())
            if wiki and _split_frontmatter(value)[0].get("type") == "index":
                raise ValueError(
                    "Runtime generates Wiki navigation indexes; submit only content files"
                )
            if draft.path.endswith(".json"):
                json.loads(value)
            if (
                self.contract.output_format == "wiki"
                and draft.path.lower().endswith(".md")
                and not wiki
            ):
                raise ValueError("Declared Wiki output requires valid OKF frontmatter")

    def is_wiki(self, path, payload):
        """Apply OKF validation only to declared Wiki output or recognized OKF page types.

        Generic Markdown may use its own frontmatter, including a different type.
        Such files and Skill package files do not acquire Wiki navigation or citations.
        """
        if self.skill_target:
            return False
        if self.contract.output_format == "files" and path.lower().endswith(".md"):
            try:
                metadata, _ = _split_frontmatter(payload.decode())
            except (ValueError, UnicodeError, yaml.YAMLError):
                return False
            if metadata.get("type") not in {
                "entity",
                "concept",
                "method",
                "comparison",
                "analysis",
                "index",
            }:
                return False
        return validate_resource_file(path, payload)

    async def save_files(self, response, group, records, old, *, origin=None):
        """Persist drafts with lineage, replacing matching paths after successful saves.

        Concurrent reducers use acceptance order: the last accepted file owns its path.
        Replacement updates recovery and catalog together without awaiting; unrelated
        files remain accepted, and failed saves retain the previous version.
        """
        output = []
        for draft in response.files:
            previous = old.get(draft.path)
            value = apply_file(draft, previous)
            inputs = draft.inputs
            artifact = {
                "path": draft.path,
                "content": value,
                "sha256": content_hash(value),
                "base_hash": content_hash(previous) if previous is not None else None,
                "owner": group.group_id,
                "origin": origin or draft.path,
                "source_refs": sorted(
                    {ref for r in records if r.record_id in inputs for ref in r.source_refs}
                ),
                "inputs": inputs,
            }
            reference = f"artifacts/{digest([group.group_id, draft.path])}"
            await self.files.put(reference, artifact)
            output.append(reference)
        supported = {record_id for draft in response.files for record_id in draft.inputs}
        for record in records:
            if self.status[record.record_id] not in {"failed", "prepared"}:
                self.status[record.record_id] = (
                    "prepared" if record.record_id in supported else "unreferenced"
                )
        await self.accept_files(output)
        return output

    async def accept_files(self, references):
        """Select saved references for publication and update their catalog entries together."""
        for reference in references:
            artifact = await self.files.get(reference)
            metadata = (
                _split_frontmatter(artifact["content"])[0]
                if self.is_wiki(artifact["path"], artifact["content"].encode())
                else {}
            )
            owner = self.owners.get(artifact["path"])
            previous = f"artifacts/{digest([owner, artifact['path']])}"
            self.artifacts = [ref for ref in self.artifacts if ref != previous]
            self.artifacts.append(reference)
            self.owners[artifact["path"]] = artifact["owner"]
            self.catalog[artifact["path"]] = {
                "path": artifact["path"],
                "title": metadata.get("title", artifact["path"]),
                "description": metadata.get("description", ""),
                "source_refs": artifact["source_refs"],
            }

    async def merge(self, references: list[str], *, partial=False) -> RenderedBundle:
        """Validate one writer per path and prepare revision-bound publication operations.

        Navigation uses code-rendered direct-child links and reads only ancestor indexes.
        Retained arbitrary pages are never enumerated or loaded; old entries are preserved.
        Partial recovery retains the same write guards. Resource recovery permits missing
        prescribed outputs; Skill packages require all declared files before publication.
        Navigation does not invoke models or Skill scripts, including during recovery.
        """
        files, owners, revisions, source_uris, origins, outputs = {}, {}, {}, {}, {}, {}
        relocations = {}
        for reference in references:
            artifact = await self.files.get(reference)
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
            if any(ref not in self.evidence for ref in artifact["source_refs"]):
                raise ValueError("Artifact refers to missing source evidence")
            source_uris[path] = sorted(
                {self.evidence[ref]["uri"] for ref in artifact["source_refs"]}
            )
            outputs[path] = {"source_refs": artifact["source_refs"]}
        existing = {}
        wiki_files = {
            path: payload for path, payload in files.items() if self.is_wiki(path, payload)
        }
        if wiki_files:
            for path, payload in wiki_files.items():
                wiki_files[path] = relocate_wiki_links(
                    payload.decode(),
                    origin=origins[path],
                    path=path,
                    target_uri=self.target,
                    relocations=relocations,
                    known_paths=set(files),
                ).encode()
            indexes: set[str] = set()
            for path in wiki_files:
                parts = path.split("/")[:-1]
                indexes.update("/".join([*parts[:i], "index.md"]) for i in range(len(parts) + 1))
            for path in sorted(indexes - set(files)):
                text = await self.load_old(path)
                if text is not None:
                    existing[path] = text.encode()
                    revisions[path] = content_hash(text)
            finalized = finalize_resource_output(
                wiki_files,
                target_uri=self.target,
                source_roots={key: info["uri"] for key, info in self.evidence.items()},
                existing_files=existing,
                known_paths=set(files) | set(existing),
                partial_catalog=True,
                source_uris_by_path=source_uris,
            )
            missing = set()
            for entry in finalized.link_report.get("unresolved", []):
                source = safe_join_viking_uri(self.target, entry["source_path"])
                uri = _link_uri(re.split(r"[#?]", entry["target"], maxsplit=1)[0], source)
                path = relative_uri_path(self.target, uri)
                if not path or path in missing:
                    continue
                try:
                    await self.client.stat(safe_join_viking_uri(self.target, path))
                except OpenVikingError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                    missing.add(path)
            if missing:
                # A missing exact target and unique matching title allow a local repair;
                # unrecalled historical pages never get redirected by basename alone.
                finalized = finalize_resource_output(
                    wiki_files,
                    target_uri=self.target,
                    source_roots={key: info["uri"] for key, info in self.evidence.items()},
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
                        self.target,
                        _link_uri(
                            re.split(r"[#?]", entry["target"], maxsplit=1)[0],
                            safe_join_viking_uri(self.target, entry["source_path"]),
                        ),
                    )
                    in missing
                ]
                if broken:
                    self.warnings.append(
                        f"Unresolved links to {len(broken)} verified missing targets."
                    )
            files.update(finalized.files)
        else:
            finalized = None
        if set(self.contract.required_paths) - set(files):
            if not partial or self.skill_target:
                raise ValueError("Missing contract-required output paths")
            self.warnings.append("Partial output is missing contract-required paths.")
        rendered = RenderedBundle()
        for path, payload in files.items():
            owners.setdefault(path, "runtime:navigation")
            if len(payload) > 8 * 1024 * 1024:
                raise ValueError("Final output including navigation/citations exceeds 8 MiB")
            uri = safe_join_viking_uri(self.target, path)
            old = self.old.get(path)
            revision = revisions.get(path)
            if revision is None and self.skill_target:
                # Detect collisions even when a plan deliberately skips history matching.
                if await self.load_old(path) is not None:
                    raise ValueError(f"Create conflicts with an existing target: {uri}")
            elif revision is not None and (old is None or content_hash(old) != revision):
                raise ValueError(f"Stale prepared artifact: {uri}")
            # Resource writes recheck even identical cached bytes under server locks;
            # concurrent changes become per-file conflicts in the publication result.
            if self.skill_target and old is not None and payload == old.encode():
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
            if self.is_wiki(path, payload):
                rendered.wiki_uris.append(uri)
        if finalized:
            rendered.link_count, rendered.link_report = finalized.link_count, finalized.link_report
            unresolved = rendered.link_report.get("unresolved", [])
            rendered.link_report["unresolved_count"] = len(unresolved)
            rendered.link_report["unresolved"] = unresolved[:20]
        for path, output in outputs.items():
            output["sha256"] = content_hash(files[path])
        await self.files.put(
            "merge", {"owners": owners, "prepared_files": len(files), "outputs": outputs}
        )
        return rendered
