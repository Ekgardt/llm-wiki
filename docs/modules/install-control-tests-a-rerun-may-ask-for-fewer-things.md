# Tests filed with `scripts/install_control.py`

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/install_state.py`: its exact derived description.
- `scripts/llm_wiki/install_release.py`: its exact derived description.
- `scripts/llm_wiki/install_requests.py`: its exact derived description.
- `scripts/llm_wiki/install_resource_builder.py`: its exact derived description.
- `scripts/llm_wiki/install_v2_transactions.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_rerun_may_ask_for_fewer_things.py -q`.
