# Tests of `scripts/flush_memory.py`

- `tests/test_a_page_belongs_to_the_project_its_evidence_names.py`: A compiled page belongs to the project its quoted blocks name (audit 2026-09-26 B-14).

`scripts/flush_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_page_belongs_to_the_project_its_evidence_names.py -q`.
