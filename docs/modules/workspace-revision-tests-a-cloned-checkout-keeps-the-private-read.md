# Tests of `scripts/workspace_revision.py`

- `tests/test_a_cloned_checkout_keeps_the_private_read.py`: The private index arms on the interpreter that runs it, and on a cloned checkout.

`scripts/workspace_revision.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_cloned_checkout_keeps_the_private_read.py -q`.
