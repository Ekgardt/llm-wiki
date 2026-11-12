# Tests of `scripts/repository_indexing.py`

- `tests/test_a_slow_worktree_does_not_end_the_pass.py`: A Git call that outlives its bound in the worktree helpers is a named refusal.

`scripts/repository_indexing.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_slow_worktree_does_not_end_the_pass.py -q`.
