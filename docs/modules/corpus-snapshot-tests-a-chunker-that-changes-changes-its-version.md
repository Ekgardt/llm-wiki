# Tests of `scripts/corpus_snapshot.py`

- `tests/test_a_chunker_that_changes_changes_its_version.py`: The chunks of two fixed sources are pinned under the extractor version's name.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_chunker_that_changes_changes_its_version.py -q`.
