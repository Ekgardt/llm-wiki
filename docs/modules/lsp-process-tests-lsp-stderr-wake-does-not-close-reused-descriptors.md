# Tests of `scripts/lsp_process.py`

`tests/test_lsp_stderr_wake_does_not_close_reused_descriptors.py` exercise `scripts/lsp_process.py` through its public names.

`scripts/lsp_process.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lsp_stderr_wake_does_not_close_reused_descriptors.py -q`.
