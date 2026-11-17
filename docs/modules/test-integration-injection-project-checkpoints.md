# `tests/test_integration_injection_project_checkpoints.py`

Project checkpoints from lifecycle events: journaled exactly once, debounced in order, batched across failure and restart, and recovered before the session-start handoff.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_integration_injection_project_checkpoints.py -q`.
