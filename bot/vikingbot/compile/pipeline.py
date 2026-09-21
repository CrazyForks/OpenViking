"""Finite Map/Shuffle/Reduce/Merge execution inside an existing Compile task."""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict
from typing import Any

from openviking.core.namespace import classify_uri
from openviking.utils.path_safety import safe_join_viking_uri
from openviking.utils.skill_processor import validate_skill_name
from openviking_cli.exceptions import OpenVikingError
from vikingbot.compile.models import CompileFailure, utc_now
from vikingbot.compile.ops import map as map_op
from vikingbot.compile.ops import merge as merge_op
from vikingbot.compile.ops import reduce as reduce_op
from vikingbot.compile.ops.shuffle import Shuffle
from vikingbot.compile.pipeline_io import JsonModel, TaskFiles
from vikingbot.compile.plan import (
    Contract,
    PlanProposal,
    Record,
    content_hash,
    digest,
    parse_plan,
)
from vikingbot.compile.renderer import RenderedBundle
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
options.overflow=structured. The default options.overflow=direct sends the complete group to Reduce
without intermediate aggregation, even above the soft batching target.
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


_SKILL_OUTPUT = """The target is a Skill namespace. Produce one complete Skill package with
all files under the same <skill-name>/ directory and a valid <skill-name>/SKILL.md.
Declare output_format=files and put that SKILL.md path in contract.options.required_paths.
Choose the package name once in the contract; all transforms use that directory.
SKILL.md requires YAML name matching its directory and a nonempty description. Preserve
attachments in their native formats. No Wiki frontmatter, citations or navigation is added
by the runtime. Use hierarchical synthesis when the Skill requires a shared entry point;
individual work sets can generate different package files.
"""


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
                    data[node.name] = await map_op.run(self, node, inputs)
                elif node.op == "shuffle":
                    data[node.name] = await shuffler.run(node, inputs)
                elif node.op == "reduce":
                    data[node.name] = await reduce_op.run(self, node, inputs)
                else:
                    if self.failures:
                        if not inputs:
                            raise ValueError("No accepted final artifacts remain after failed jobs")
                        self.warnings.append(
                            "Partial output; unfinished jobs: " + self.failures[0][:500]
                        )
                    result = await merge_op.run(self, inputs, partial=bool(self.failures))
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
            return "prepared"

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
