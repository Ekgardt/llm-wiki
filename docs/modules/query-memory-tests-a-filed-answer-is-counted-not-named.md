# Tests of `scripts/query_memory.py`

- `tests/test_a_filed_answer_is_counted_not_named.py`: A filed answer's page is named in the tracked log only when it is published.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_filed_answer_is_counted_not_named.py -q`.
