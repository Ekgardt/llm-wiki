# Tests of `scripts/queue_core.py`

- `tests/test_a_dead_capture_gets_its_second_chance_after_a_fix.py`: A dead capture gets one redrive after the code changes, and the purge keeps what it cannot prove.

`scripts/queue_core.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_dead_capture_gets_its_second_chance_after_a_fix.py -q`.
