# Tests of `scripts/lsp_profiles.py`

- `tests/test_the_typescript_engine_confirms_itself_after_initialize.py`: The engine a server says it loaded reaches the session, and is judged.

`scripts/lsp_profiles.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_typescript_engine_confirms_itself_after_initialize.py -q`.
