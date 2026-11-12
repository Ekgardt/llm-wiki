# Tests of `scripts/capture_diagnostics.py`

- `tests/test_an_expired_trail_lock_never_authorizes_an_append.py`: An unavailable sidecar is a refused append, never permission to lose a line.

`scripts/capture_diagnostics.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_expired_trail_lock_never_authorizes_an_append.py -q`.
