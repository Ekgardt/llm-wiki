# `tests/test_profile_discovery_never_imports_the_installer.py`

Its test reads the Pyright profile source and proves discovery never imports or calls the installer.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_profile_discovery_never_imports_the_installer.py -q`.
