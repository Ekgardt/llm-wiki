# Tests of `scripts/lsp_profiles.py`

- `tests/test_a_document_is_opened_in_its_own_language.py`: Audit 3, B14: `didOpen` names the document's own language, not always Python.

`scripts/lsp_profiles.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_document_is_opened_in_its_own_language.py -q`.
