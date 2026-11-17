# Tests of `scripts/evidence_resolver.py`

- `tests/test_a_page_is_read_past_one_bad_reference.py`: A page that mentions `daily:` in prose is read, the mention named (audit 2026-09-26 B-17).

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_page_is_read_past_one_bad_reference.py -q`.
