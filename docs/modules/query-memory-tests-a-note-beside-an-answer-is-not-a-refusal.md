# Tests of `scripts/query_memory.py`

- `tests/test_a_note_beside_an_answer_is_not_a_refusal.py`: A complete answer must not be destroyed for a note written beside it.

`scripts/query_memory.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_note_beside_an_answer_is_not_a_refusal.py -q`.
