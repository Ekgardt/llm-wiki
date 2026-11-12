# Tests of `scripts/markdown_transaction.py`

- `tests/test_an_append_keeps_only_what_it_added.py`: An append keeps only the bytes it added, and still undoes and recovers exactly.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_append_keeps_only_what_it_added.py -q`.
