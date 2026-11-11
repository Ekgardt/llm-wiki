# Tests of `scripts/codex_hook_identity.py`

`tests/test_codex_capture_hooks.py` exercise `scripts/codex_hook_identity.py` through its public names.

`scripts/codex_hook_identity.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_codex_capture_hooks.py -q`.
