# Tests of `scripts/llm_wiki/markdown_targets.py`

- `tests/test_windows_parent_handles_are_held_and_released.py`

`scripts/llm_wiki/markdown_targets.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_windows_parent_handles_are_held_and_released.py -q`.
