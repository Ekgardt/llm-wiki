# Tests of `scripts/generation_catalog.py`

- `tests/test_the_stand_keys_the_turns_before_it_builds.py`: On the stand a turn is found under its keys, because it is keyed before the build.

`scripts/generation_catalog.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_stand_keys_the_turns_before_it_builds.py -q`.
