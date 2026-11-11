# Tests of `scripts/memory_state.py`

`tests/test_a_definition_and_a_service_walk_say_what_they_left_out.py` exercise `scripts/memory_state.py` through its public names.

`scripts/memory_state.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_definition_and_a_service_walk_say_what_they_left_out.py -q`.
