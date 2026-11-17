# Tests of `scripts/llm_wiki/doctor_lsp.py`

- `tests/test_doctor_lsp_rejects_malformed_records.py`

`scripts/llm_wiki/doctor_lsp.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_doctor_lsp_rejects_malformed_records.py -q`.
