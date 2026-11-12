# Tests of `scripts/impact_analysis.py`

- `tests/test_impact_reads_what_the_diff_really_holds.py`: Audit 3, A13/A14/B31/B32: insertions, large graphs, submodules and symlinks.

`scripts/impact_analysis.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_impact_reads_what_the_diff_really_holds.py -q`.
