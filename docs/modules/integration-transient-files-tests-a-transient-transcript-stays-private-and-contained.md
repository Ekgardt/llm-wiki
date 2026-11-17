# Tests of `scripts/llm_wiki/integration_transient_files.py`

- `tests/test_a_transient_transcript_stays_private_and_contained.py`

`scripts/llm_wiki/integration_transient_files.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_transient_transcript_stays_private_and_contained.py -q`.
