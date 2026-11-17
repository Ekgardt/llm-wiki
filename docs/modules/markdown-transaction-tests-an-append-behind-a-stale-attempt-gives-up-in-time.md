# Tests of `scripts/markdown_transaction.py`

- `tests/test_an_append_behind_a_stale_attempt_gives_up_in_time.py`: An append that cannot get past a stale `preparing` attempt stops at its deadline.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_append_behind_a_stale_attempt_gives_up_in_time.py -q`.
