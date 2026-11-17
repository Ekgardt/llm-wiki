# Tests filed with `scripts/install_control.py`

- `tests/test_an_update_honors_explicit_resource_adoption.py`: Explicit takeover during an update retains the reviewed current baseline.

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/install_state.py`: its exact derived description.
- `scripts/llm_wiki/install_profile.py`: its exact derived description.
- `scripts/llm_wiki/install_records.py`: its exact derived description.
- `scripts/llm_wiki/install_uninstall_rollback.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_update_honors_explicit_resource_adoption.py -q`.
