# Model retry design evidence

This directory preserves the original design experiment and pre-change evidence.
The integrated implementation is in `openviking/utils/model_call.py`; its current
scope, regressions and release gates are recorded in
[`docs/design/model-retry-governance-zh.md`](../../docs/design/model-retry-governance-zh.md).
Production code does not import this prototype.

## Run

The policy acceptance suite uses only Python's standard library:

```sh
python3 -m unittest discover -s test_scripts/model_retry -p 'test_*.py' -v
```

The replay requires an existing OpenViking development environment, including
`httpx` and `volcengine-python-sdk`. It must import a clean checkout or archive of
commit `611f5c469b2bb8dc6d072b215251379e780d3f23`, not the migrated working tree:

```sh
PYTHONPATH=/path/to/clean/baseline python test_scripts/model_retry/replay.py --output /tmp/model-retry-baseline.json
```

The recorded baseline uses commit `611f5c469b2bb8dc6d072b215251379e780d3f23` and
`volcengine-python-sdk==5.0.14`. Assertions intentionally detect changes to those
behaviors. All model HTTP traffic goes through `httpx.MockTransport`, with dummy
credentials. Backoff sleeps are disabled in the replay; it measures request
counts, not latency or cost.

## What was exercised

| Fixture | Baseline HTTP requests | Prototype HTTP requests |
| --- | ---: | ---: |
| Persistent Embedding 429, one delivery | 12 | 4 |
| Persistent VLM 401, one credential | 4 | 1 |
| Persistent VLM 429, Phase-2 retry helper composed with real adapter | 16 | 4 |
| Same composition with two credentials | 32 | 4 total across routes |
| Online persistent 429 | Not measured as a full online request | 1 |

The baseline calls the real Volcengine adapter and installed SDK. The Phase-2
fixture composes its real retry helper and retry constant with the real adapter;
it does not execute an entire Session commit. The proposed side calls the real
SDK with SDK retries disabled, under the prototype owner. It does not patch a
production adapter to use the prototype.

The real `TextEmbeddingHandler` is also exercised with a fake queue/backend.
Transient, unknown, content-safety and quota errors return `requeued` across
three observed deliveries. Auth fails terminally. For quota exhaustion the
breaker opens after the first model-boundary call, so subsequent observed
deliveries wait/requeue without another model call. The test is deliberately
bounded; absence of a total cap is established by source inspection, not by
claiming that a test executed infinitely.

## Contract and limits

`prototype.py` demonstrates a single async retry owner, online/offline policy,
one shared attempt cap across configured credentials, absolute deadlines,
request timeouts, cancellation, cooperative JSON budget handoff, and an optional
in-process quota for **extra** requests. Twenty ordinary logical calls still
get twenty first attempts when the retry quota is zero.

Credential selection in this sketch is illustrative, not a final routing policy.
Auth/quota failover is opt-in, offline-only, and never retries the same failed
credential. The event sink is a Python list, not a deployed Prometheus collector.

This experiment does **not** implement durable budget reservation, crash recovery,
distributed quota coordination, SDK-wide conformance, the production queue/workflow
integration, or actual token billing. A JSON snapshot is not a transaction with
an external model request. Reloading a stale snapshot can still reset consumed
budget; rollout must not claim a hard lifetime request cap until that boundary is
implemented and tested.

The environment reported an unavailable native RAGFS binding during import.
These adapter/handler fixtures do not exercise that binding, so their passing
results are not evidence of full filesystem or QueueFS E2E behavior.

## Integrated implementation checks

```sh
PYTHONPATH=. python -m pytest --no-cov -q \
  tests/unit/test_model_call.py \
  tests/models/test_model_retry_transport.py \
  tests/storage/test_model_retry_terminal.py \
  tests/unit/session/test_session_commit_resume.py \
  tests/metrics/integration/test_model_retry.py
```

These exercise real adapter/SDK HTTP requests with MockTransport, real queue
handlers, Phase-2 failure handling with in-memory storage, and the actual metric
router/exporter. They do not prove native-engine E2E or durable crash budgets.
The baseline report is historical evidence, not a measurement of this branch.

## Review corrections after PR head `176fa9304`

The owner now recognizes structured `insufficient_quota` codes/types and removes
exhausted credentials from the call. VikingDB sync/async HTTP errors preserve the
response instead of returning empty vectors. Semantic overview and file-summary
paths propagate terminal model errors, cancel and drain failed batches, and settle
the consumer without requeue. Shared workers restore each executor's workload,
stage and deadline. Sync calls reject results that arrive after the deadline;
interrupting blocking I/O still depends on the transport timeout.

Validation on macOS/Python 3.13.5, OpenAI 2.24.0, google-genai 2.25.0, httpx 0.28.1:

- Before the fix, 47 assertions failed across quota, VikingDB transport, Semantic
  outcome/context and sync deadline cases; the pre-deadline control passed.
- Final expanded regression: **600 passed, 2 failed, 2 skipped**. This includes
  42 Gemini tests, the provider transport suites, owner/breaker/credential
  tests, Session resume, Semantic consumers/executors, collection schemas and
  metrics. Both failures reproduce on clean pre-fix head `176fa9304`: the
  `test_memory_directory_summarizes_all_uncached_files` and
  `test_memory_directory_vectorizes_changed_files_with_generated_summary`
  fixtures omit the existing `total_files` argument.
- `test_semantic_processor_permanent_storage_error.py` was excluded after its
  collection failed on an import of the already-removed `semantic_dag` module;
  that import is also present in the pre-fix head.
- All 14 standalone prototype cases pass from the relocated directory. Ruff
  checks/formatting and `git diff --check` pass. Mypy on the five changed source
  files reports the same 36 diagnostics as `176fa9304`, with no additions.
- Native E2E stops at preflight: `native vector engine is unavailable`. No native
  or STG success is claimed for this correction; the earlier native results
  below remain historical evidence for their recorded revision.

The expanded command uses an **absolute** `PYTHONPATH` so the clean-process queue
import check can change directories without losing the source checkout:

```sh
PYTHONPATH="$PWD" python -m pytest --no-cov -q --tb=short \
  tests/unit/test_model_call.py tests/unit/test_model_retry.py \
  tests/unit/test_circuit_breaker.py tests/unit/test_failover_embedder_permanent_advance.py \
  tests/unit/test_gemini_embedder.py tests/unit/session/test_session_commit_resume.py \
  tests/models/test_model_retry_transport.py tests/models/test_cohere_retry_transport.py \
  tests/models/test_gemini_retry_transport.py tests/models/test_minimax_retry_transport.py \
  tests/storage/test_model_retry_terminal.py tests/storage/test_collection_schemas.py \
  tests/storage/test_queue_manager.py tests/metrics/integration/test_model_retry.py \
  $(rg --files tests/storage -g 'test_semantic*.py' \
    -g '!test_semantic_processor_permanent_storage_error.py')
```

## Native service and queue validation

`native_e2e.py` runs an actual in-process OpenViking service with native RAGFS,
SQLite QueueFS, filesystem PathLock and the local vector engine. It does not use
pytest fixtures or replace native storage. It requires an installed OpenViking
runtime with working native bindings, and uses the real OpenAI SDK against a
loopback-only HTTP fixture. No real provider credentials are required.

```sh
PYTHONPATH=. python test_scripts/model_retry/native_e2e.py \
  --case resource-success --output /tmp/resource-success.json
```

Run each case in a separate process: `resource-success`,
`resource-embedding-429`, `resource-embedding-401`, `session-success`,
`session-vlm-429`, `session-vlm-401`, and `resource-cancel`.
The JSON records task state, physical HTTP requests, model events, queue drain,
archive markers and resource lock reacquisition. Persistent 429 allows four
attempts per logical call; 401 allows one. Initialization vectors are drained
and counted separately before fault injection. Resource success also checks
file/directory retry stages while preserving the existing Token stage.

The session fixture enables working-memory summary and disables long-term
extraction. It verifies actual Phase 1 delivery and the SessionCommit consumer,
not ExtractLoop protocols. The 120-second test timeout is not production policy.
This does not cover HTTP ingress, multiple Pods, Redis, process crashes, real
model interoperability or billing. Record both the Python source revision and
native runtime image when using binaries built from another revision; a passing
test does not establish that those binaries were rebuilt from the tested source.
