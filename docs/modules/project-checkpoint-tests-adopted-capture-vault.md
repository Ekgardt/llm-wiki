# Tests of `scripts/project_checkpoint.py`

- `tests/adopted_capture_vault.py`: A temporary vault whose capture queue is adopted, with the adapter pointed at it.

`scripts/project_checkpoint.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/adopted_capture_vault.py -q`.
