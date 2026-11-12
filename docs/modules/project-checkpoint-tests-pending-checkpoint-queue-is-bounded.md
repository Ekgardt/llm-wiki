# Tests of `scripts/project_checkpoint.py`

- `tests/test_pending_checkpoint_queue_is_bounded.py`: One project may not hold back events until `run/state.json` is unreadable.

`scripts/project_checkpoint.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_pending_checkpoint_queue_is_bounded.py -q`.
