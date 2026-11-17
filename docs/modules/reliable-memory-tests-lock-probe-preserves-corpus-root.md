# Tests of `scripts/reliable_memory.py`

- `tests/test_lock_probe_preserves_corpus_root.py`: Real locking diagnostics must not invalidate a live corpus ancestor seal.

`scripts/reliable_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lock_probe_preserves_corpus_root.py -q`.
