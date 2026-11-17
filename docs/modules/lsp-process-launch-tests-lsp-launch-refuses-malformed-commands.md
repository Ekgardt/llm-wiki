# Tests of `scripts/llm_wiki/lsp_process_launch.py`

- `tests/test_lsp_launch_refuses_malformed_commands.py`

`scripts/llm_wiki/lsp_process_launch.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lsp_launch_refuses_malformed_commands.py -q`.
