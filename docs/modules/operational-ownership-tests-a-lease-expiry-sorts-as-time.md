# Tests of `scripts/operational_ownership.py`

- `tests/test_a_lease_expiry_sorts_as_time.py`: A lease written on a whole second expires on that second, not up to a second later.

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_lease_expiry_sorts_as_time.py -q`.
