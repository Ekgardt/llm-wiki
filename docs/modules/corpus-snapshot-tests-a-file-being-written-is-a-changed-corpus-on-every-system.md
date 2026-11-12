# Tests of `scripts/corpus_snapshot.py`

- `tests/test_a_file_being_written_is_a_changed_corpus_on_every_system.py`: A source written while it is read by path is a changed corpus, not an unsafe path.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_file_being_written_is_a_changed_corpus_on_every_system.py -q`.
