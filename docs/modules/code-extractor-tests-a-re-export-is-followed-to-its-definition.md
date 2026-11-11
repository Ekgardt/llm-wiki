# Tests of `scripts/code_extractor.py`

- `tests/test_a_re_export_is_followed_to_its_definition.py`: A call through a package re-export reaches the definition (audit 2026-09-26 B-6).

`scripts/code_extractor.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_re_export_is_followed_to_its_definition.py -q`.
