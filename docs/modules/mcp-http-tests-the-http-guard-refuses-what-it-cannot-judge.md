# Tests of `scripts/mcp_http.py`

- `tests/test_the_http_guard_refuses_what_it_cannot_judge.py`: The HTTP guard answers every request it cannot accept with a refusal (audit 2026-09-27 C-14).

`scripts/mcp_http.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_http_guard_refuses_what_it_cannot_judge.py -q`.
