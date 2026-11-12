# `benchmark/retrieval_v2_sqlite_lexical.py`

The SQLite FTS5 lexical adapter and its owned, deadline-bounded index.

This module owns `benchmark/retrieval_v2_sqlite_lexical.py` and `tests/test_retrieval_v2_benchmark.py`.
It is one concern of the retrieval v2 benchmark runner. The runner was split out of
`benchmark/run_retrieval_v2.py` on 2026-11-10 into the `benchmark/retrieval_v2_*.py`
concern modules; its command line, report fields, worker protocol and validations did
not change. `benchmark_runner_sha256` now digests every runner source file named by
`RUNNER_SOURCES` in `benchmark/retrieval_v2_environment.py`, not the facade alone. The
modules import each other by bare name, and a name a test replaces is read through its
defining module, so the replacement is seen.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_retrieval_v2_benchmark.py -q`.
