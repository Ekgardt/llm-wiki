# Tests of `scripts/generation_fts_validation.py`

- `tests/test_a_verified_digest_is_remembered.py`: A digest already verified is remembered by stat identity, Git's way.

`scripts/generation_fts_validation.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_verified_digest_is_remembered.py -q`.
