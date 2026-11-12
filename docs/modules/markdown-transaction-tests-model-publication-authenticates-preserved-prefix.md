# Tests of `scripts/markdown_transaction.py`

- `tests/test_model_publication_authenticates_preserved_prefix.py`: Unchanged authenticated history is not newly authored provider output.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_model_publication_authenticates_preserved_prefix.py -q`.
