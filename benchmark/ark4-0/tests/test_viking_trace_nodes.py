import copy

import pytest
from viking_trace import collect_trace, node_messages, normalize_turn


def node(name, **info):
    return {"node_name": name, "trace_info": info}


def legacy(*, direct=False):
    calls = (
        []
        if direct
        else [
            node(
                "think",
                input={"messages": [{"role": "system", "content": "SECRET_PROMPT"}]},
                tool_calls={"tool_calls": [{"name": "search", "id": "call1", "args": {"q": "q"}}]},
            ),
            node("search", input={"q": "q"}, output={"hits": ["evidence"]}),
        ]
    )
    return {
        "user_input": {"query": {"raw": "original question"}},
        "node_list": [
            node(
                "agent_server.predict.request",
                sub_nodes=[
                    node(
                        "chat_react",
                        output={"messages": [{"role": "system", "content": "SECRET_PROMPT"}]},
                        sub_nodes=calls,
                    ),
                    node(
                        "summarizer",
                        output={"full_message" if direct else "content": "final answer"},
                    ),
                ],
            )
        ],
    }


def test_node_tools_and_final_answer_without_prompt_context():
    messages = node_messages(legacy())
    assert [m["role"] for m in messages] == ["user", "assistant", "assistant"]
    assert messages[1]["parts"][0]["tool_input"] == {"q": "q"}
    assert messages[1]["parts"][0]["tool_output"] == '{"hits": ["evidence"]}'
    assert messages[-1]["parts"][0]["text"] == "final answer"
    assert "SECRET_PROMPT" not in str(messages)


def test_direct_reply_does_not_require_fake_tools():
    messages, fmt = normalize_turn([legacy(direct=True)])
    assert fmt == "nodes"
    assert len(messages) == 2


def test_tool_failure_is_kept_as_evidence_not_a_missing_trace():
    trace = legacy()
    call_nodes = trace["node_list"][0]["trace_info"]["sub_nodes"][0]["trace_info"]["sub_nodes"]
    call_nodes[1]["trace_info"]["output"] = {"error": {"code": "TOOL_SERVER_TOOL_EXEC_ERROR"}}
    messages = node_messages(trace)
    assert "TOOL_SERVER_TOOL_EXEC_ERROR" in messages[1]["parts"][0]["tool_output"]


@pytest.mark.parametrize("missing", ["query", "answer", "result", "wrong_args"])
def test_incomplete_node_trace_stays_blocked(missing):
    trace = legacy()
    children = trace["node_list"][0]["trace_info"]["sub_nodes"]
    if missing == "query":
        trace["user_input"] = {}
    elif missing == "answer":
        children.pop()
    elif missing == "result":
        children[0]["trace_info"]["sub_nodes"].pop()
    else:
        children[0]["trace_info"]["sub_nodes"][1]["trace_info"]["input"] = {"q": "other"}
    with pytest.raises(ValueError):
        normalize_turn([trace])


def test_repeated_calls_match_distinct_results_and_skip_engine_marker():
    trace = legacy()
    children = trace["node_list"][0]["trace_info"]["sub_nodes"][0]["trace_info"]["sub_nodes"]
    pair = copy.deepcopy(children)
    pair[0]["trace_info"]["tool_calls"]["tool_calls"][0]["id"] = "call2"
    pair[1]["trace_info"]["output"] = "second result"
    children.extend(
        [
            node(
                "think",
                tool_calls={
                    "tool_calls": [
                        {"name": "_engine_continue", "id": "engine", "args": {"reason": "continue"}}
                    ]
                },
            ),
            *pair,
        ]
    )
    tools = [p for m in node_messages(trace) for p in m["parts"] if p["type"] == "tool"]
    assert [p["tool_id"] for p in tools] == ["call1", "call2"]
    assert tools[1]["tool_output"] == "second result"


def test_worker_reply_preferred_to_main_acknowledgement():
    main = legacy(direct=True)
    main["node_list"][0]["trace_info"]["sub_nodes"][-1]["trace_info"]["output"] = {
        "full_message": "working on it"
    }
    messages, _ = normalize_turn([main, legacy()])
    assert "working on it" not in str(messages)
    assert messages[-1]["parts"][0]["text"] == "final answer"


def test_canonical_format_still_preferred():
    trace = {
        "session_trace": {
            "messages": [
                {
                    "info": {"id": "m", "sessionID": "s", "role": "assistant"},
                    "parts": [{"type": "text", "text": "canonical answer"}],
                }
            ]
        }
    }
    messages, fmt = normalize_turn([legacy(), trace])
    assert fmt == "session"
    assert len(messages) == 1
    assert messages[0]["parts"][0]["text"] == "canonical answer"


@pytest.mark.asyncio
async def test_collect_node_trace_and_preserve_fail_closed_errors():
    class Client:
        async def tos_files(self, prefix):
            return [{"name": "main.json", "key": prefix + "/main.json"}]

        async def tos_json(self, path):
            return legacy()

    result = {"output": {"trace_tos_path": "tos://bucket/vaka_operator/1/r/traces"}}
    messages, info = await collect_trace(Client(), result, [], lambda *_: None)
    assert len(messages) == 3
    assert info["trace_errors"] == []
    assert list(info["trace_formats"].values()) == ["nodes"]

    class Broken(Client):
        async def tos_json(self, path):
            return {"node_list": []}

    messages, info = await collect_trace(Broken(), result, [], lambda *_: None)
    assert messages == []
    assert info["trace_errors"]
