# `tests/test_lsp_security.py`

Its tests hold a repository source to its checkout: lexical paths, retained handles, symlinks, junctions, short names and provider URIs.

It also owns `tests/lsp_security_helpers.py`, the helpers other test files import from it.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_lsp_security.py -q`.
