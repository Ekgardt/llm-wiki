# `tests/test_workspace_revision_hashing_and_git_bounds.py`

Its tests cover the inventory hint, owned-descriptor hashing, rename pairing, non-git manifests, final fences, bounded git output, process-tree cleanup, deadlines and size ceilings.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_workspace_revision_hashing_and_git_bounds.py -q`.
