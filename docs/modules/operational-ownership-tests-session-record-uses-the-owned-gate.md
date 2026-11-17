# Tests of `scripts/operational_ownership.py`

- `tests/test_session_record_uses_the_owned_gate.py`: A queued capture keeps its session record instead of losing it silently.

`scripts/operational_ownership.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_session_record_uses_the_owned_gate.py -q`.
