# Tests of `scripts/lsp_process.py`

- `tests/test_a_retained_cleanup_owner_leaves_the_module_registry.py`: A session that takes over a startup cleanup takes it out of the registry.

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_retained_cleanup_owner_leaves_the_module_registry.py -q`.
