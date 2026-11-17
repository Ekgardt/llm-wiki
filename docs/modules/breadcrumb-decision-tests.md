# Tests of `scripts/breadcrumb_decision.py`

- `tests/compile_keeps_complete_tool_lines_helpers.py`: Helpers shared by test_compile_keeps_complete_tool_lines.py and the test files importing them.
- `tests/native_user_frames_keep_their_physical_evidence_helpers.py`: Helpers shared by test_native_user_frames_keep_their_physical_evidence.py and the test files importing them.

`scripts/breadcrumb_decision.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/compile_keeps_complete_tool_lines_helpers.py tests/native_user_frames_keep_their_physical_evidence_helpers.py -q`.
