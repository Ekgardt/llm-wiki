# Tests of `scripts/llm_wiki/install_transactions.py`

- `tests/test_a_first_format_install_refuses_what_it_cannot_prove.py`

`scripts/llm_wiki/install_transactions.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_first_format_install_refuses_what_it_cannot_prove.py -q`.
