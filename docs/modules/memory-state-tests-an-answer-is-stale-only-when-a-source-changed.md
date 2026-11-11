# Tests of `scripts/memory_state.py`

`tests/test_an_answer_is_stale_only_when_a_source_changed.py` exercise `scripts/memory_state.py` through its public names.

`scripts/memory_state.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_answer_is_stale_only_when_a_source_changed.py -q`.
