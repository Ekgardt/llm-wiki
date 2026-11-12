# Tests of `scripts/cleanup_worktrees.py`

- `tests/test_a_repository_read_runs_no_config_command.py`: Reading a repository runs no program its own config names.

`scripts/cleanup_worktrees.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_repository_read_runs_no_config_command.py -q`.
