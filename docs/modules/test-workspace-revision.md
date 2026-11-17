# `tests/test_workspace_revision.py`

Its tests pin the public contract of a workspace revision and the basic change, containment and verification answers.

It also owns `tests/workspace_revision_helpers.py`, the helpers other test files import from it.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_workspace_revision.py -q`.
