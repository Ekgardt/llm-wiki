# Tests of `scripts/query_memory.py`

- `tests/test_a_reply_is_pruned_to_what_bears_on_the_question.py`: Real context delivery retains the pruned source spans and their hashes.

`scripts/query_memory.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_reply_is_pruned_to_what_bears_on_the_question.py -q`.
