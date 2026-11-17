# Tests of `scripts/evidence_resolver.py`

- `tests/test_a_split_day_is_archived_with_every_part.py`: A day compiled in several parts archives with every part's receipt (audit 2026-09-26 B-1).

`scripts/evidence_resolver.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_split_day_is_archived_with_every_part.py -q`.
