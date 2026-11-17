# `tests/test_integration_injection.py`

The installers' guarded steps on every platform: the smoke run's exit status, timeouts, signals and
test-tree cleanup, the MCP function and Codex hook probes in a temporary home, the official Codex hooks
and the one external state root.

The CI installer job selects these with `-k installer` on Linux, Windows and macOS; the other
integration scenarios live in `tests/test_integration_injection_*.py`.

`tests/integration_injection_case_helpers.py` holds the helpers the split scenario files share
and belongs to this module.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_integration_injection.py -q`.
