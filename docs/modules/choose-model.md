# Select one subscription model for all memory work in llm-wiki.toml

`scripts/choose_model.py` and the tests that exercise them most.

Catalog entries are candidates, not entitlement evidence. A selected model must complete one protected short request before its choice is atomically saved.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_model_selection_test_provider.py tests/test_the_installer_asks_which_model.py -q`.
