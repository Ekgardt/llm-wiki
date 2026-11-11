# Tests of `scripts/workspace_revision.py`

- `tests/test_a_failed_git_status_is_a_navigation_degradation.py`: A Git command the revision runs that exits non-zero is a `ValueError` the navigation degrades on.

`scripts/workspace_revision.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_failed_git_status_is_a_navigation_degradation.py -q`.
