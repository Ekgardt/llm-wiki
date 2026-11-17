# Tests filed with `scripts/capture_diagnostics.py`

- `tests/test_a_long_session_in_the_vault_is_captured.py`: A long session in the vault is captured, and each capture keeps its own window.

These files do not import `scripts/capture_diagnostics.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/episode_consolidation.py`: its exact derived description.
- `scripts/integration_adapter.py`: its exact derived description.
- `scripts/session_evidence.py`: its exact derived description.
- `scripts/user_prompt_capture.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_long_session_in_the_vault_is_captured.py -q`.
