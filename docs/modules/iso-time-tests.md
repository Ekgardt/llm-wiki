# Tests of `scripts/iso_time.py`

- `tests/test_a_writer_past_its_lease_is_named.py`: A live process holding the writer gate past its lease is named (audit 2026-09-27 C-3).

`scripts/iso_time.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_writer_past_its_lease_is_named.py -q`.
