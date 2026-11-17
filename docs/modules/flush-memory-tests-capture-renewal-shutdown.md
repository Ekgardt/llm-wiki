# Tests of `scripts/flush_memory.py`

- `tests/test_capture_renewal_shutdown.py`: Authority must outlive an in-flight renewal on success and on failure.

`scripts/flush_memory.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_capture_renewal_shutdown.py -q`.
