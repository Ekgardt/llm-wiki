# Comparative adapter subprocess fixture

Owns `benchmark/fixtures/comparative_adapter_fixture.py` at its existing path,
with direct subprocess qualification and recorded baseline payloads. The source
is byte-identical. Its six adapter names and numerical metrics are deliberately
synthetic fixtures for `tests/test_comparative_benchmark.py`; they establish
no real comparative quality, latency, memory or token efficiency.

The fixture accepts one adapter argument and one JSON object on stdin, then
prints the existing sorted JSON response. `probe-model` prints the fixed model
label without reading a request. Missing, extra or unknown adapter arguments
return code 2. Malformed JSON fails normally. The existing graphify-pinned /
fixture-task-a / seed 1729 / attempt 1 combination emits one retryable failure;
subsequent attempts emit the normal synthetic success. No persistent state,
model, HTTP call or runtime root is involved.

Direct tests compare all successful adapter responses and the failure payload
with complete baseline JSON, and exercise retry/probe/refusal paths through real
Python subprocesses. Existing consumer integration assertions remain unchanged
in `tests/test_comparative_benchmark.py` and are run alongside these checks. The
whole runner remains a separate responsibility, with its own eventual packet.

The full packet retains the actual pytest bootstrap, source, tests, golden JSON,
this guide and the complete September 12 fixture refactoring history. Import
guards refuse product/runtime dependencies. Actual CCN, branch shape, Ruff and
exact reconstruction under 100000 o200k_base tokens remain required.

## Decision evidence — 2026-11-03

The approved autonomous plan permits qualifying a cohesive existing file in
place. Absorbing the runner would mix synthetic fixture behavior with execution
and evaluation; fragmenting this small fixture adds indirection. Existing
source, commands, metrics and integration tests remain unchanged.

Primary sources reviewed: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[pytest fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html), and
[Python 3.14 subprocess IO](https://docs.python.org/3.14/library/subprocess.html).
These inform explicit responsibilities and real-process qualification; they do
not prescribe this project-specific package boundary or validate fixture metrics.
