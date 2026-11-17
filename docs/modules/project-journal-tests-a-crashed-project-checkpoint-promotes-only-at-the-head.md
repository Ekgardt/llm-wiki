# Tests of `scripts/project_journal.py`

- `tests/test_a_crashed_project_checkpoint_promotes_only_at_the_head.py`

`scripts/project_journal.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_crashed_project_checkpoint_promotes_only_at_the_head.py -q`.
