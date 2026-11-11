# Tests of `scripts/integration_adapter.py`

`tests/breadcrumb_worker_helpers.py` exercise `scripts/integration_adapter.py` through its public names.

`scripts/integration_adapter.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/breadcrumb_worker_helpers.py -q`.
