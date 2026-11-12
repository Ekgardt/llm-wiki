# Tests of `scripts/query_memory.py`

- `tests/test_the_window_holds_whole_entries_and_a_count_fans_out.py`: A selected piece brings its whole entry; a count fans out before it is final.

`scripts/query_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_window_holds_whole_entries_and_a_count_fans_out.py -q`.
