# Tests filed with `scripts/install_control.py`

- `tests/test_the_scheduler_outlasts_the_pass.py`: The Windows nightly task's time limit sits above the pass's own worst case.

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/install_scheduler_render.py`: its exact derived description.
- `scripts/nightly_budget.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_scheduler_outlasts_the_pass.py -q`.
