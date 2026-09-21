"""Task-local embedding candidates and scoped historical recall for Compile Shuffle."""

from __future__ import annotations

import asyncio
import heapq
import json
import math
from array import array
from collections.abc import Sequence
from threading import Event

from openviking.core.namespace import relative_uri_path
from vikingbot.compile.ops.common import job
from vikingbot.compile.pipeline_io import bounded_jobs
from vikingbot.compile.plan import Group, Record, RouteResponse, digest


def top_candidates(
    vectors: Sequence[Sequence[float]], k: int = 5, stop: Event | None = None
) -> list[list[int]]:
    """Compute top-k cosine candidates in 64x256 blocks, never an N by N matrix.

    Similarity ranks candidates only; links for joint processing are selected separately.
    Vector validation applies independently of
    collection size; cancellation is checked between processing blocks.
    """
    if not vectors:
        return []
    try:
        import numpy as np
    except ImportError:
        # The stdlib path keeps only each row's top-k candidates.
        rows = []
        for vector in vectors:
            norm = math.sqrt(sum(x * x for x in vector))
            if not norm or not math.isfinite(norm) or len(vector) != len(vectors[0]):
                raise ValueError("Invalid dense embedding vectors")
            rows.append([x / norm for x in vector])
        result = []
        for i, row in enumerate(rows):
            if stop and stop.is_set():
                raise ValueError("Similarity calculation cancelled")
            pairs = (
                (sum(a * b for a, b in zip(row, other, strict=True)), -j)
                for j, other in enumerate(rows)
                if i != j
            )
            result.append([-j for _, j in heapq.nlargest(k, pairs)])
        return result
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        raise ValueError("Embedding vectors must have equal dimensions and finite values")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Zero embeddings cannot supply semantic candidates")
    matrix /= norms
    size = len(matrix)
    k = min(k, max(0, size - 1))
    result = []
    for start in range(0, size, 64):
        if stop and stop.is_set():
            raise ValueError("Similarity calculation cancelled")
        block_rows = matrix[start : start + 64]
        best = np.full((len(block_rows), k), -np.inf, dtype=np.float32)
        indices = np.full((len(block_rows), k), -1, dtype=np.int64)
        for offset in range(0, size, 256):
            scores = block_rows @ matrix[offset : offset + 256].T
            ids = np.broadcast_to(np.arange(offset, offset + scores.shape[1]), scores.shape)
            scores[ids == np.arange(start, start + len(block_rows))[:, None]] = -np.inf
            values = np.concatenate((best, scores), axis=1)
            candidates = np.concatenate((indices, ids), axis=1)
            selected = np.argsort(-values, axis=1, kind="stable")[:, :k]
            best = np.take_along_axis(values, selected, axis=1)
            indices = np.take_along_axis(candidates, selected, axis=1)
        result.extend(indices.tolist())
    return result


def split_group(keys, vectors):
    """Partition IDs into disjoint batches of at most 50 using existing nonzero vectors.

    Each seed takes its nearest cosine neighbours; ties preserve input order.
    Groups of at most 50 retain their membership and order without recomputation.
    """
    if len(keys) <= 50:
        return [keys]
    norms = {key: math.sqrt(sum(x * x for x in vectors[key])) for key in keys}
    remaining, groups = dict.fromkeys(keys), []
    while remaining:
        seed = next(iter(remaining))
        del remaining[seed]
        neighbours = heapq.nlargest(
            49,
            remaining,
            key=lambda key: (
                sum(a * b for a, b in zip(vectors[seed], vectors[key], strict=True)) / norms[key]
            ),
        )
        groups.append([seed, *neighbours])
        for key in neighbours:
            del remaining[key]
    return groups


def group_related_records(keys, links):
    """Partition IDs into groups whose members all have direct candidate links.

    Either link direction suffices. Unknown endpoints are rejected; input order
    determines first-fit assignment, and records without links remain independent.
    """
    neighbours = {key: set() for key in keys}
    for left, right in links:
        neighbours[left].add(right)
        neighbours[right].add(left)
    groups = []
    for key in neighbours:
        for group in groups:
            if all(member in neighbours[key] for member in group):
                group.append(key)
                break
        else:
            groups.append([key])
    return groups


class Shuffle:
    """Build joint work sets from global vector candidates and scoped historical recall.

    Paths and scope descriptions are evidence, never equality gates or output claims.
    Reduce resolves factual differences and chooses the output files for each work set.
    """

    def __init__(self, runtime):
        self.r = runtime
        self.search_cache = {}
        self.descriptions = {}
        self.model_id = ""

    async def vectors(self, records: list[Record]) -> list[array]:
        """Return compact vectors in record order using bounded, model-scoped cached batches."""
        r = self.r
        metadata = await r.client.compile_embeddings([], target_uri=r.target)
        self.model_id = metadata["model"]
        texts = [
            # Scope contributes semantic context without excluding differently worded records.
            (record.routing_text + " " + json.dumps(record.scope, ensure_ascii=False))[:1024]
            for record in records
        ]
        vectors = {}
        missing = []
        for text in dict.fromkeys(texts):
            key = digest([self.model_id, text])
            cached = await r.files.get(f"embeddings/{key}")
            if cached is not None:
                vectors[text] = array("f", cached)
                r.metrics["embedding_cache_hits"] += 1
            else:
                missing.append(text)

        async def embed(batch):
            """Use the shared provider limits and retain compact float32 routing vectors."""
            result = await r.client.compile_embeddings(
                batch, target_uri=r.target, expected_model=self.model_id
            )
            if result["model"] != self.model_id or len(result["vectors"]) != len(batch):
                raise ValueError("Embedding batch/model mismatch")
            r.metrics["embedding_batches"] += 1
            for text, vector in zip(batch, result["vectors"], strict=True):
                vectors[text] = array("f", vector)
                await r.files.put(f"embeddings/{digest([self.model_id, text])}", vector)

        await bounded_jobs(
            (missing[start : start + 32] for start in range(0, len(missing), 32)),
            embed,
            concurrency=r.limits.source_concurrency,
            metrics=r.metrics,
        )
        return [vectors[text] for text in texts]

    async def candidates(self, text: str, stable_uri=None) -> list[dict]:
        """Recall only inside to; coalesce section/derived hits to visible parent files.

        A directory contributes at most four direct children. No recursive listing
        or full-history body read is performed. Transport failures remain failures.
        """
        r = self.r
        key = (text, stable_uri)
        if key in self.search_cache:
            r.metrics["search_cache_hits"] += 1
            return self.search_cache[key]
        entries = []
        if stable_uri:
            if not relative_uri_path(r.target, stable_uri):
                raise ValueError("Stable target candidate is outside to")
            entries.append({"uri": stable_uri})
        if not stable_uri:
            result = await r.client.find(text, target_uri=r.target, limit=4)
            if hasattr(result, "to_dict"):
                result = result.to_dict()
            category = "skills" if r.skill_target else "resources"
            if not isinstance(result, dict) or category not in result:
                raise ValueError("Malformed historical search response")
            entries.extend(result[category])
        seen = {}
        for entry in entries[:4]:
            uri = str(entry.get("uri", "")).split("#", 1)[0].rstrip("/")
            if uri.endswith(("/.abstract.md", "/.overview.md")):
                uri = uri.rsplit("/", 1)[0]
            if uri == r.target:
                continue
            if not relative_uri_path(r.target, uri):
                raise ValueError(f"Search returned an out-of-scope URI: {uri}")
            stat = await r.client.stat(uri)
            if stat.get("isDir"):
                children = (
                    [{"uri": uri + "/SKILL.md", "abstract": entry.get("abstract", "")}]
                    if r.skill_target
                    else await r.client.list_resources(uri, node_limit=4)
                )
            else:
                children = [{"uri": uri, "abstract": entry.get("abstract", "")}]
            for child in children:
                page = str(child.get("uri", ""))
                if not relative_uri_path(r.target, page):
                    raise ValueError(f"Search directory returned an out-of-scope URI: {page}")
                if child.get("isDir") or any(
                    p.startswith(".") for p in relative_uri_path(r.target, page).split("/")
                ):
                    continue
                if page not in self.descriptions:
                    abstract = str(child.get("abstract") or "")[:800]
                    self.descriptions[page] = (
                        abstract or (await r.client.read_raw(page, limit=16))[:800]
                    )
                    r.metrics["history_descriptions"] += 1
                seen[page] = {"uri": page, "description": self.descriptions[page]}
                if len(seen) >= 4:
                    break
            if len(seen) >= 4:
                break
        r.metrics["history_candidates"] += len(seen)
        self.search_cache[key] = list(seen.values())
        return self.search_cache[key]

    async def run(self, node, records: list[Record]) -> list[Group]:
        """Assign every record to one work set, allowing cross-path and cross-scope links.

        Independent batches compare global candidates, including later records.
        Only failed batches remain unfinished; successful records continue grouping.
        Unconfirmed candidates never become links. Recall failures
        are recorded without fabricating empty history; cancellation propagates.
        """
        if not records:
            return []
        r = self.r
        vectors = await self.vectors(records)
        stop = Event()
        try:
            neighbours = await asyncio.to_thread(top_candidates, vectors, 5, stop)
        finally:
            stop.set()
        vectors = dict(zip((record.record_id for record in records), vectors, strict=True))
        r.metrics["similarity_records"] += len(records)
        r.metrics["similarity_pairs"] += len(records) * (len(records) - 1)

        def describe(record):
            return {
                "record": record.record_id,
                "text": record.routing_text,
                "scope": record.scope,
                "sources": sorted({r.evidence[ref]["uri"] for ref in record.source_refs})[:3],
            }

        async def route(indices):
            assigned = [records[i] for i in indices]
            data, evidence, candidates = [], {}, {}
            for index in indices:
                record = records[index]
                nearby = [records[i] for i in neighbours[index]]
                history = (
                    await self.candidates(record.routing_text, stable_uri=record.target_uri)
                    if node.against_target
                    else []
                )
                data.append(
                    {
                        **describe(record),
                        "candidates": [other.record_id for other in nearby],
                        "history": history,
                    }
                )
                candidates.update((other.record_id, describe(other)) for other in nearby)
                for other in [record, *nearby]:
                    evidence[other.record_id] = {
                        "id": other.record_id,
                        "payload": {
                            "source_ranges": other.source_refs,
                        },
                    }

            history_uris = {entry["uri"] for item in data for entry in item["history"]}

            def validate(response):
                if sorted(d.record for d in response.decisions) != sorted(
                    x.record_id for x in assigned
                ):
                    raise ValueError("Routing must account for every supplied record exactly once")
                for decision in response.decisions:
                    if not set(decision.related) <= evidence.keys():
                        raise ValueError(
                            "Joint processing must refer to records shown in this request"
                        )
                    if not set(decision.history) <= history_uris:
                        raise ValueError("Historical context must name a recalled file")

            async def decide():
                response = await r.model.ask(
                    "route",
                    r.system
                    + "\nSHUFFLE: "
                    + getattr(r.contract, node.task)
                    + "\nSelect any records or candidates shown in this request that need joint "
                    "synthesis, comparison or "
                    "deduplication. Preserve potential supplements, versions and contradictions "
                    "together; Reduce resolves their facts and applicability. Different proposed "
                    "paths or scope wording do not prevent joint processing. Broad topical "
                    "similarity alone does not require linking independent material. If identity "
                    "is uncertain but joint inspection is needed, include the candidate. Select "
                    "any recalled history shown in this request that needs comparison; "
                    "it need not be updated. Do not "
                    "choose output paths, merge facts or exclude records. One work set can "
                    "produce several files. Return one decision per supplied record.",
                    {
                        "records": data,
                        "scope_fields": r.contract.distinguish,
                        "candidates": candidates,
                        "inputs": list(evidence.values()),
                    },
                    RouteResponse,
                    validate,
                )
                return response.decisions

            return await job(
                r,
                f"{node.name}-batch-{digest([item.record_id for item in assigned])[:16]}",
                assigned,
                decide,
            )

        batches = ([index] for index in range(len(records)))
        results = await bounded_jobs(
            batches,
            route,
            concurrency=r.limits.source_concurrency,
            metrics=r.metrics,
            failures=r.failures,
        )
        links, targets = [], {}
        for decisions in results:
            for decision in decisions:
                links.extend((decision.record, other) for other in decision.related)
                targets[decision.record] = decision.history
                await r.files.put(f"routes/{node.name}-{decision.record}", decision.model_dump())
        # Missing routing decisions exclude only their own records, not successful neighbours.
        by_id = {record.record_id: record for record in records if record.record_id in targets}
        r.metrics["route_blocked_records"] += len(records) - len(by_id)
        links = [(left, right) for left, right in links if left in by_id and right in by_id]
        groups, components = [], []
        for ids in group_related_records(by_id, links):
            components.extend(await asyncio.to_thread(split_group, ids, vectors))
        for ids in components:
            group = Group(
                digest([node.name, sorted(ids)])[:24],
                [by_id[key] for key in ids],
                sorted({uri for key in ids for uri in targets[key]}),
            )
            await r.files.put(
                f"groups/{group.group_id}",
                {
                    "records": ids,
                    "target_uris": group.target_uris,
                },
            )
            groups.append(group)
        return groups
