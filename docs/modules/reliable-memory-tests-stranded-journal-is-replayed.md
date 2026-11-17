# Tests of `scripts/reliable_memory.py`

- `tests/test_stranded_journal_is_replayed.py`: A crashed writer's rollback journal fenced off the whole memory write path.

`scripts/reliable_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_stranded_journal_is_replayed.py -q`.
