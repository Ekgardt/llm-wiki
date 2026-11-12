# `tests/test_install_pyright.py`

Its tests download, verify and unpack the pinned managed Pyright artifact within bounds.

It also owns `tests/pyright_installer_helpers.py`, the helpers other test files import from it.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_install_pyright.py -q`.
