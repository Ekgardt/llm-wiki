# Tests of `scripts/impact_analysis.py`

- `tests/test_an_impact_git_child_is_never_orphaned.py`: A deadline that has passed starts no Git child for impact analysis (audit C-40).

`scripts/impact_analysis.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_impact_git_child_is_never_orphaned.py -q`.
