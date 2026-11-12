# Tests of `scripts/mcp_server.py`

- `tests/test_an_answer_without_a_provider_is_not_fresh.py`: A navigation answer is fresh only when it was computed from the current revision (audit 2026-09-27 C-19).

`scripts/mcp_server.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_answer_without_a_provider_is_not_fresh.py -q`.
