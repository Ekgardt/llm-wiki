# Tests of `scripts/workspace_revision.py`

- `tests/test_navigation_dead_code_and_stale_words.py`: Navigation keeps its word: dead-code narrowing and the caller's Git budget (audit C-44).

`scripts/workspace_revision.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_navigation_dead_code_and_stale_words.py -q`.
