# Tests of `scripts/memory_state.py`

- `tests/test_a_quarantined_day_stays_pending.py`: The mirror names only receipted days, and a day an old quarantine hid is offered again.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_quarantined_day_stays_pending.py -q`.
