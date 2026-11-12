# `tests/test_memory_queue_worker.py`

The bounded queue worker: default and overridden policy, elapsed and idle limits, handler timeouts that kill the whole child tree, result frames drained without deadlock, and cleanup failures that block the task.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_memory_queue_worker.py -q`.
