# Tests of `scripts/reliable_memory.py`

- `tests/test_one_file_one_bound.py`: A capture decision has one size bound, for its writer and every reader (audit 2026-09-27 B-3).

`scripts/reliable_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_file_one_bound.py -q`.
