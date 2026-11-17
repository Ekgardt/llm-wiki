# `benchmark/run_retrieval_v2.py`

The command-line entry point of the retrieval v2 benchmark, and the worker script the
selection orchestrator launches by its path. Since 2026-11-10 it holds no logic: it
re-exports `main` from `benchmark/retrieval_v2_cli.py`, and the implementation lives in
the `benchmark/retrieval_v2_*.py` concern modules beside it, each with its own guide
(`benchmark-retrieval-v2-<concern>.md`). The command line, report fields, worker protocol
and validations are unchanged. `benchmark_runner_sha256` now digests every file named by
`RUNNER_SOURCES` in `benchmark/retrieval_v2_environment.py` — this facade and every
concern module — so a change to any of them changes the runner identity a report and the
bound baseline policy record.

This module owns `benchmark/run_retrieval_v2.py` and `tests/test_retrieval_v2_facade.py`.
The test keeps `RUNNER_SOURCES` equal to the facade plus every `retrieval_v2_*.py` file, so
a new concern module cannot escape the runner identity.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_retrieval_v2_facade.py -q`.
