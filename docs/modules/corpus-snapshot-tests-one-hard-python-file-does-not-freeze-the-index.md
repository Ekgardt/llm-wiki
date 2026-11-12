# Tests of `scripts/corpus_snapshot.py`

- `tests/test_one_hard_python_file_does_not_freeze_the_index.py`: A Python file too deep to parse is one file's parse error, not the index's end.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_hard_python_file_does_not_freeze_the_index.py -q`.
