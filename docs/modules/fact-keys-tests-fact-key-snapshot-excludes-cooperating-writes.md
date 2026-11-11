# Tests of `scripts/fact_keys.py`

- `tests/test_fact_key_snapshot_excludes_cooperating_writes.py`: Snapshot exclusion ends before a provider call; errors remain errors.

`scripts/fact_keys.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_fact_key_snapshot_excludes_cooperating_writes.py -q`.
