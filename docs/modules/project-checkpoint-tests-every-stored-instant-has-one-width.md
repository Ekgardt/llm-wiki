# Tests of `scripts/project_checkpoint.py`

- `tests/test_every_stored_instant_has_one_width.py`: A stored instant's text order is its time order (audit 2026-09-26 C-12).

`scripts/project_checkpoint.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_stored_instant_has_one_width.py -q`.
