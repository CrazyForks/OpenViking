"""Extract Agent sessions or node-based actions, never internal LLM prompts."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse


def trace_nodes(trace):
    """Walk only the execution graph, not embedded prompts or tool responses."""
    for node in trace.get("node_list", []):
        yield node
        yield from trace_nodes({"node_list": node.get("trace_info", {}).get("sub_nodes", [])})


def node_messages(trace):
    """Recover real calls/results and the delivered reply from legacy workflow traces.

    chat_react.messages/think.input contain expanded system context, so are
    deliberately not imported. Missing results or a missing final reply fail closed.
    """
    nodes = list(trace_nodes(trace))
    query = trace.get("user_input", {}).get("query", {}).get("raw")
    if not isinstance(query, str) or not query:
        raise ValueError("Node trace missing original query")
    identity = hashlib.sha256(json.dumps(trace, sort_keys=True).encode()).hexdigest()[:24]
    messages = [
        {"id": f"{identity}:user", "role": "user", "parts": [{"type": "text", "text": query}]}
    ]
    used = set()
    for index, node in enumerate(nodes):
        if node.get("node_name") != "think":
            continue
        calls = node.get("trace_info", {}).get("tool_calls", {}).get("tool_calls", [])
        for call in calls:
            name = call.get("name")
            if not name or "args" not in call or not call.get("id"):
                raise ValueError("Node trace has malformed tool call")
            # Routing is not a user task tool; the actual reply is in summarizer.
            if name in {"dispatch_async_task_event", "_engine_continue"}:
                continue
            match = None
            for j in range(index + 1, len(nodes)):
                candidate = nodes[j]
                if candidate.get("node_name") == "think":
                    break
                info = candidate.get("trace_info", {})
                if (
                    j not in used
                    and candidate.get("node_name") == name
                    and info.get("input") == call["args"]
                    and "output" in info
                ):
                    match = j
                    break
            if match is None:
                raise ValueError(f"Node trace missing result for tool {name}")
            used.add(match)
            output = nodes[match]["trace_info"]["output"]
            messages.append(
                {
                    "id": f"{identity}:tool:{index}:{call['id']}",
                    "role": "assistant",
                    "parts": [
                        {
                            "type": "tool",
                            "tool_id": call["id"],
                            "tool_name": name,
                            "tool_input": call["args"],
                            "tool_output": output
                            if isinstance(output, str)
                            else json.dumps(output, ensure_ascii=False),
                            "tool_status": "completed",
                        }
                    ],
                }
            )
    answer = None
    for node in nodes:
        if node.get("node_name") == "summarizer":
            output = node.get("trace_info", {}).get("output", {})
            if isinstance(output, dict):
                answer = output.get("full_message") or output.get("content") or answer
    if not isinstance(answer, str) or not answer:
        raise ValueError("Node trace missing final reply")
    messages.append(
        {
            "id": f"{identity}:answer",
            "role": "assistant",
            "parts": [{"type": "text", "text": answer}],
        }
    )
    return messages


def normalize_turn(traces):
    if any(s.get("messages") for t in traces for s in sessions(t)):
        return normalize_messages(traces), "session"
    # Prefer the actual worker trace over the main dispatch/acknowledgement trace.
    workers = [
        t
        for t in traces
        if any(
            c.get("name") not in {"dispatch_async_task_event", "_engine_continue"}
            for n in trace_nodes(t)
            if n.get("node_name") == "think"
            for c in n.get("trace_info", {}).get("tool_calls", {}).get("tool_calls", [])
        )
    ]
    selected = workers or traces
    if not selected:
        raise ValueError("No trace files found")
    return [m for t in selected for m in node_messages(t)], "nodes"


def sessions(value):
    if isinstance(value, dict):
        trace = value.get("session_trace")
        if isinstance(trace, dict) and isinstance(trace.get("messages"), list):
            yield trace
        for key, child in value.items():
            if key != "session_trace":
                yield from sessions(child)
    elif isinstance(value, list):
        for child in value:
            yield from sessions(child)


def normalize_messages(traces):
    """Later session snapshots replace earlier copies of the same message."""
    messages = {}
    snapshots = [session for trace in traces for session in sessions(trace)]
    for session in sorted(snapshots, key=lambda s: s.get("exported_at_ms") or 0):
        for message in session["messages"]:
            info = message.get("info", {})
            if info.get("role") not in {"user", "assistant"}:
                continue
            parts = []
            for part in message.get("parts", []):
                if part.get("type") == "text" and part.get("text"):
                    parts.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "tool":
                    state = part.get("state", {})
                    output = state.get("output", state.get("error", ""))
                    parts.append(
                        {
                            "type": "tool",
                            "tool_id": part.get("callID", part.get("id", "")),
                            "tool_name": part.get("tool", ""),
                            "tool_input": state.get("input"),
                            "tool_output": output
                            if isinstance(output, str)
                            else json.dumps(output, ensure_ascii=False),
                            "tool_status": state.get("status", "pending"),
                        }
                    )
            if not parts:
                continue
            identity = str(info.get("sessionID", "")) + ":" + str(info.get("id", ""))
            if identity == ":":
                identity = hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
            messages[identity] = {"id": identity, "role": info["role"], "parts": parts}
    return list(messages.values())


def trace_prefixes(result, logs):
    """Per-row logs retain earlier turns; the result exposes only the final turn."""
    prefixes = []
    for log in reversed(logs):  # Viking returns newest first.
        text = json.dumps(log, ensure_ascii=False)
        prefixes.extend(re.findall(r"tos://[\w.-]+/vaka_operator/\d+/[\w-]+/traces", text))
    output = result.get("output") or {}
    if output.get("trace_tos_path"):
        prefixes.append(output["trace_tos_path"].rstrip("/"))
    return list(dict.fromkeys(prefixes))


async def collect_trace(client, result, logs, save):
    prefixes = trace_prefixes(result, logs)
    messages, sources, errors, formats = [], [], [], {}
    for prefix in prefixes:
        traces = []
        try:
            files = await client.tos_files(prefix)
            # Main and async traces may both contain session snapshots. Ignore markdown.
            for item in sorted(files, key=lambda item: item["name"]):
                if not item["name"].endswith(".json"):
                    continue
                key = item["key"]
                path = (
                    key
                    if key.startswith("tos://")
                    else f"tos://{urlparse(prefix).netloc}/{key.lstrip('/')}"
                )
                try:
                    trace = await client.tos_json(path)
                    name = hashlib.sha256(path.encode()).hexdigest()
                    save(name, trace)
                    traces.append(trace)
                    sources.append(path)
                except Exception as exc:
                    errors.append(
                        {"path": path, "error": type(exc).__name__ + ": " + str(exc).split("?")[0]}
                    )
        except Exception as exc:
            errors.append({"path": prefix, "error": type(exc).__name__})
        try:
            turn_messages, trace_format = normalize_turn(traces)
            messages.extend(turn_messages)
            formats[prefix] = trace_format
        except ValueError as exc:
            errors.append({"path": prefix, "error": str(exc)})
    if not messages:
        errors.append({"error": "No recoverable Agent messages found"})
    # Canonical snapshots can repeat earlier messages across turn exports.
    messages = list({m["id"]: m for m in messages}.values())
    return messages, {
        "trace_sources": sources,
        "trace_prefixes": prefixes,
        "trace_errors": errors,
        "trace_formats": formats,
    }
