# Tests of `scripts/query_memory.py`

- `tests/test_a_quote_is_checked_by_string_not_by_a_model.py`: Quote, then answer: the citation check is a string match on the quote.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_quote_is_checked_by_string_not_by_a_model.py -q`.
