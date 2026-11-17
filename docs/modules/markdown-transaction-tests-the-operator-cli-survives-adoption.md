# Tests of `scripts/markdown_transaction.py`

- `tests/test_the_operator_cli_survives_adoption.py`: The transaction CLI must open the database the vault actually has.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_operator_cli_survives_adoption.py -q`.
