# Tests of `scripts/mcp_server.py`

- `tests/test_an_answer_is_fresh_by_its_own_sources.py`: An answer is fresh by its own sources, not by every file in the vault (audit 2026-09-27 B-10).

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_answer_is_fresh_by_its_own_sources.py -q`.
