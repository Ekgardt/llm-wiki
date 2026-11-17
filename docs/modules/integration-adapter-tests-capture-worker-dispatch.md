# Tests of `scripts/integration_adapter.py`

`tests/test_capture_worker_dispatch.py` exercise `scripts/integration_adapter.py`.

`scripts/integration_adapter.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_capture_worker_dispatch.py -q`.
