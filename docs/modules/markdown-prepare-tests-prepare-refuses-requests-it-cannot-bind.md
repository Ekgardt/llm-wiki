# Tests of `scripts/llm_wiki/markdown_prepare.py`

- `tests/test_prepare_refuses_requests_it_cannot_bind.py`

`scripts/llm_wiki/markdown_prepare.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_prepare_refuses_requests_it_cannot_bind.py -q`.
