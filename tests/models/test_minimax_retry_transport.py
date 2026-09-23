# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Exercise requests/urllib3, including response headers, over actual HTTP."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests

from openviking.models.embedder.minimax_embedders import MinimaxDenseEmbedder
from openviking.utils.model_call import get_model_call_error, model_workload


@pytest.fixture
def minimax_transport(monkeypatch):
    events = []
    responses = []
    sent = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            status, headers = responses[min(len(sent), len(responses) - 1)]
            sent.append(status)
            events.append(("request", status))
            body = json.dumps({"base_resp": {"status_code": 0}, "vectors": [[0.1, 0.2]]}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    worker.start()
    model = MinimaxDenseEmbedder(
        api_key="fixture-not-a-secret",
        api_base=f"http://127.0.0.1:{server.server_port}/embeddings",
        dimension=2,
    )
    # Only replace the owner's clock. The requests/urllib3 transport is real.
    monkeypatch.setattr(
        "openviking.utils.model_call.time",
        SimpleNamespace(time=time.time, sleep=lambda delay: events.append(("sleep", delay))),
    )
    monkeypatch.setattr("openviking.utils.model_call.random.uniform", lambda *_: 0)
    try:
        yield model, responses, sent, events
    finally:
        model.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("status", [429, 503])
def test_minimax_preserves_retry_after_beyond_wait_limit(minimax_transport, status):
    model, responses, sent, events = minimax_transport
    responses.append((status, {"Retry-After": "60"}))

    with model_workload("add_resource"), pytest.raises(RuntimeError) as caught:
        model.embed("fixture")

    terminal = get_model_call_error(caught.value)
    assert terminal is not None
    assert (terminal.reason, terminal.attempts) == ("backoff_limit", 1)
    cause = caught.value.__cause__
    assert isinstance(cause, requests.HTTPError)
    assert cause.response.status_code == status
    assert cause.response.headers["Retry-After"] == "60"
    assert sent == [status]
    assert events == [("request", status)]


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("retry_after", [2, 30])
def test_minimax_owner_waits_before_recovery(minimax_transport, status, retry_after):
    model, responses, sent, events = minimax_transport
    responses.extend([(status, {"Retry-After": str(retry_after)}), (200, {})])

    with model_workload("add_resource"):
        result = model.embed("fixture")

    assert result.dense_vector == [0.1, 0.2]
    assert sent == [status, 200]
    assert events == [("request", status), ("sleep", retry_after), ("request", 200)]


@pytest.mark.parametrize(
    "workload,status,expected,reason",
    [
        ("offline", 429, 4, "max_attempts"),
        ("offline", 503, 4, "max_attempts"),
        ("offline", 401, 1, "auth"),
        ("online", 429, 1, "online"),
    ],
)
def test_minimax_transport_has_no_extra_attempts(
    minimax_transport, workload, status, expected, reason
):
    model, responses, sent, events = minimax_transport
    responses.append((status, {}))

    with model_workload("add_resource", workload=workload), pytest.raises(RuntimeError) as caught:
        model.embed("fixture")

    terminal = get_model_call_error(caught.value)
    assert terminal is not None
    assert (terminal.reason, terminal.attempts) == (reason, expected)
    assert sent == [status] * expected
    assert sum(event == "sleep" for event, _ in events) == expected - 1
