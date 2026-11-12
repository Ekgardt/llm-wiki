# Tests of `scripts/llm_wiki/sqlite_transactions.py`

- `tests/test_sqlite_transaction_compatibility.py`: Historical imports and serialized function identities remain compatible.

`scripts/llm_wiki/sqlite_transactions.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_sqlite_transaction_compatibility.py -q`.
