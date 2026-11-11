# Tests of `scripts/project_journal.py`

- `tests/test_a_rebuild_unblocks_the_project_it_was_run_for.py`: After `--rebuild`, the checkpoint that asked for the rebuild settles and the project moves on.

`scripts/project_journal.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_rebuild_unblocks_the_project_it_was_run_for.py -q`.
