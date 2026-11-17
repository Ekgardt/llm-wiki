# Tests filed with `scripts/project_journal.py`

- `tests/test_a_crashed_project_checkpoint_promotes_only_at_the_head.py`

These files do not import `scripts/project_journal.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/markdown_transaction.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_crashed_project_checkpoint_promotes_only_at_the_head.py -q`.
