# Tests of `scripts/doctor.py`

- `tests/test_a_check_is_judged_when_it_ends.py`: A store found broken before the deadline stays an error, whatever a later check costs.

`scripts/doctor.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_check_is_judged_when_it_ends.py -q`.
