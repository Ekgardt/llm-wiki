# `tests/test_memory_queue.py`

Behavior tests for the fenced SQLite deferred-work queue: payloads, source fences, dedupe, claims,
retries, cancellation, schema and the deferred handlers.

Result publication and the queue's file hardening live in `tests/test_memory_queue_results.py`.

`tests/memory_queue_scenario_helpers.py` holds the clock and queue fixtures and the helpers the queue scenario files share, and belongs to this module.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_memory_queue.py -q`.
