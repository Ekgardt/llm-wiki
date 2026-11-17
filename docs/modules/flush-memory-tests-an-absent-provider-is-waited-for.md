# Tests of `scripts/flush_memory.py`

- `tests/test_an_absent_provider_is_waited_for.py`: An absent provider is a stated wait; a task's last failure is recorded as a loss.

`scripts/flush_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_absent_provider_is_waited_for.py -q`.
