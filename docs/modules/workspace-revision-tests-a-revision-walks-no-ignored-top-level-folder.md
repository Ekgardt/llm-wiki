# Tests of `scripts/workspace_revision.py`

- `tests/test_a_revision_walks_no_ignored_top_level_folder.py`: A top-level folder git ignores whole is not walked by the workspace revision.

`scripts/workspace_revision.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_revision_walks_no_ignored_top_level_folder.py -q`.
