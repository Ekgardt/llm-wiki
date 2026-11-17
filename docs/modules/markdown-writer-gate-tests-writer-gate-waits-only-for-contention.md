# Tests of `scripts/llm_wiki/markdown_writer_gate.py`

- `tests/test_writer_gate_waits_only_for_contention.py`

`scripts/llm_wiki/markdown_writer_gate.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_writer_gate_waits_only_for_contention.py -q`.
