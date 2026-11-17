# Tests of `scripts/lsp_process.py`

- `tests/test_an_unread_proc_entry_never_calls_a_group_inert.py`: Four places where missing or stale evidence was treated as a settled answer.

`scripts/lsp_process.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_unread_proc_entry_never_calls_a_group_inert.py -q`.
