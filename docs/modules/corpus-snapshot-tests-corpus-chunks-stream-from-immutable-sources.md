# Tests of `scripts/corpus_snapshot.py`

- `tests/test_corpus_chunks_stream_from_immutable_sources.py`: A derived corpus cannot retain every expanded chunk object.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_corpus_chunks_stream_from_immutable_sources.py -q`.
