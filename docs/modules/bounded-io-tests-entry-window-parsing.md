# Tests of `scripts/bounded_io.py`

- `tests/test_entry_window_parsing.py`: Entry grammar stays identical when verified blocks are parsed in memory.

`scripts/bounded_io.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_entry_window_parsing.py -q`.
