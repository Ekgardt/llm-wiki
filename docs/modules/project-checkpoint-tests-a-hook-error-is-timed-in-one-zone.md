# Tests of `scripts/project_checkpoint.py`

- `tests/test_a_hook_error_is_timed_in_one_zone.py`: A hook error is judged by the moment it happened, whatever the zone (audit 2026-09-26 B-25).

`scripts/project_checkpoint.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_hook_error_is_timed_in_one_zone.py -q`.
