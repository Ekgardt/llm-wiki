# Tests of `scripts/scheduled_nightly.py`

- `tests/test_a_housekeeping_step_names_its_failure.py`: A step after the counted ones names its failure instead of ending the night.

`scripts/scheduled_nightly.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_housekeeping_step_names_its_failure.py -q`.
