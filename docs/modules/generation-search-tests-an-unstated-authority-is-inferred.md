# Tests of `scripts/generation_search.py`

- `tests/test_an_unstated_authority_is_inferred.py`: A knowledge page that states no source_authority ranks as inferred (audit 2026-09-27 C-15).

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_unstated_authority_is_inferred.py -q`.
