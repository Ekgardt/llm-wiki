# `tests/test_memory_queue_legacy_names.py`

The queue's compatibility names: re-exports and the heartbeat, timing, process-cleanup and child
runtime names are the owners' objects, and contract classes and values keep their legacy pickle bytes,
also across a real spawn.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_memory_queue_legacy_names.py -q`.
