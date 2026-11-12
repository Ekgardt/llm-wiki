# `tests/test_code_navigation_query_fence.py`

Its tests prove a navigation query validates before it runs, keeps each attempt inside one revision fence and deadline, and never publishes stale or partial facts.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_code_navigation_query_fence.py -q`.
