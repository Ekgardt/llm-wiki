# `tests/test_workspace_revision_private_index_races.py`

The private-index fast path of a revision check survives races, cancellation and failures, and never returns a stale True.

It also owns `tests/workspace_revision_race_helpers.py`, the helper its git-process sibling shares.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_workspace_revision_private_index_races.py -q`.
