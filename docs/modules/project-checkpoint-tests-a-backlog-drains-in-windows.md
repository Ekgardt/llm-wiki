# Tests of `scripts/project_checkpoint.py`

- `tests/test_a_backlog_drains_in_windows.py`: A backlog must not make the cost of draining it grow with its own size.

`scripts/project_checkpoint.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_backlog_drains_in_windows.py -q`.
