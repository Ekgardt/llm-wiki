# Tests of `scripts/memory_state.py`

- `tests/test_the_adoption_pass_says_what_it_skipped.py`: The adoption pass looks past intents it cannot adopt, and the worker names them.

`scripts/memory_state.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_adoption_pass_says_what_it_skipped.py -q`.
