# Tests of `scripts/llm_wiki/install_profile.py`

- `tests/test_the_profile_block_is_recognized_rewritten_and_removed_in_place.py`

`scripts/llm_wiki/install_profile.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_profile_block_is_recognized_rewritten_and_removed_in_place.py -q`.
