# Tests of `scripts/process_liveness.py`

- `tests/a_dead_owner_root_is_swept_when_the_next_server_starts_helpers.py`: Helpers shared by test_a_dead_owner_root_is_swept_when_the_next_server_starts.py and the test files importing them.
- `tests/test_process_namespace_ownership.py`: A missing PID in another namespace is not proof that its owner died.

`scripts/process_liveness.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_dead_owner_root_is_swept_when_the_next_server_starts_helpers.py tests/test_process_namespace_ownership.py -q`.
