# `tests/test_memory_queue_cli.py`

Operator CLI tests for memory_queue: list, status, cancel, redrive, purge and quarantine output,
the work command's defaults and overrides, canonical failure codes and the state-free process probe.

The bounded worker itself is tested in `tests/test_memory_queue_worker.py`.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_memory_queue_cli.py -q`.
