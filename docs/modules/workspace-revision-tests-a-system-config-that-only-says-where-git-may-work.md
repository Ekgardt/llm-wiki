# Tests of `scripts/workspace_revision.py`

- `tests/test_a_system_config_that_only_says_where_git_may_work.py`: A `[safe]` section in a system git config does not turn the private read off.

`scripts/workspace_revision.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_system_config_that_only_says_where_git_may_work.py -q`.
