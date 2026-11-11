# Tests of `scripts/code_extractor.py`

- `tests/test_a_local_name_hides_an_import.py`: A name the function binds itself hides an import of it (audit 2026-09-26 C-9).

`scripts/code_extractor.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_local_name_hides_an_import.py -q`.
