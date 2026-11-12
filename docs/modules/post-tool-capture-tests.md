# Tests of `scripts/post_tool_capture.py`

`tests/test_a_tool_call_in_the_vault_is_captured.py` exercise `scripts/post_tool_capture.py` through its public names.

`scripts/post_tool_capture.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_tool_call_in_the_vault_is_captured.py -q`.
