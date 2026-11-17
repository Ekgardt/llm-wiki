# Tests of `scripts/operational_ownership.py`

- `tests/test_a_dead_owner_is_reclaimed_without_its_marker.py`: A dead nightly owner whose marker is gone or replaced can still be reclaimed.

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_dead_owner_is_reclaimed_without_its_marker.py -q`.
