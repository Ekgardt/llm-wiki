# Tests of `scripts/queue_core.py`

- `tests/test_a_childs_tree_is_read_when_it_is_killed.py`: The processor child's `R` means "the task is done", and its tree is read then.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_childs_tree_is_read_when_it_is_killed.py -q`.
