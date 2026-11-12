# Tests of `scripts/code_navigation.py`

- `tests/test_an_unclaimed_file_is_unsupported.py`: A file no managed server claims is unsupported, not an error (audit 2026-09-26 C-9).

`scripts/code_navigation.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_unclaimed_file_is_unsupported.py -q`.
