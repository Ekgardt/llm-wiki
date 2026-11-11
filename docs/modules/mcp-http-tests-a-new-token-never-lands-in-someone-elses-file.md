# Tests of `scripts/mcp_http.py`

- `tests/test_a_new_token_never_lands_in_someone_elses_file.py`: Audit 3, B6: minting a token never writes through what already sits at its path.

`scripts/mcp_http.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_new_token_never_lands_in_someone_elses_file.py -q`.
