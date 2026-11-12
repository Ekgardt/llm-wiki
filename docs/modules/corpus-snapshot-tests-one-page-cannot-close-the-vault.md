# Tests of `scripts/corpus_snapshot.py`

- `tests/test_one_page_cannot_close_the_vault.py`: One page with unreadable metadata costs its metadata, never the corpus.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_page_cannot_close_the_vault.py -q`.
