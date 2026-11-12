# Tests of `scripts/fact_keys.py`

- `tests/test_a_key_reply_is_read_as_written.py`: A key reply is read under the names it used.

`scripts/fact_keys.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_key_reply_is_read_as_written.py -q`.
