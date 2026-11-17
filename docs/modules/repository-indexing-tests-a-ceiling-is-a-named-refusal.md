# Tests of `scripts/repository_indexing.py`

- `tests/test_a_ceiling_is_a_named_refusal.py`: A repository past an extraction ceiling is a named refusal, not a crash (audit 2026-09-26 B-11).

`scripts/repository_indexing.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_ceiling_is_a_named_refusal.py -q`.
