# `tests/test_code_navigation.py`

Its tests pin the navigation records, the binding of a navigation to one session and repository, revision reuse, the source-document cache and location deduplication.

It also owns `tests/code_navigation_query_helpers.py`, the helpers other test files import from it.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_code_navigation.py -q`.
