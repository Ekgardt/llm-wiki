# Tests of `scripts/reflection.py`

- `tests/test_the_weekly_task_outlasts_its_pass.py`: The Windows weekly task's limit sits above the weekly pass's own worst case.

`scripts/reflection.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_weekly_task_outlasts_its_pass.py -q`.
