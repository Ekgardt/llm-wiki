# Tests filed with `scripts/install_control.py`

- `tests/test_the_guide_matches_the_passes.py`: The user guide says what the passes do and the limits they run under; no hint drops extras.

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/llm_wiki/install_scheduler_render.py`: its exact derived description.
- `scripts/weekly_budget.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_guide_matches_the_passes.py -q`.
