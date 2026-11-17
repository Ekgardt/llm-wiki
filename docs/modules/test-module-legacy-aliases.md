# `tests/test_module_legacy_aliases.py`

Cross-owner aliases and serialization are integration qualification: the path-ancestry readers, the
SQLite I/O records and the PEM redaction helper keep one implementation and their pickle bytes.

The queue's and the ownership and Markdown records' aliases live in
`tests/test_module_legacy_aliases_*.py`.

`tests/module_legacy_alias_helpers.py` holds the repository root and the definition reader the legacy alias files share, and belongs to this module.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_module_legacy_aliases.py -q`.
