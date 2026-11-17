# Tests filed with `scripts/install_control.py`

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/doctor.py`: its exact derived description.
- `scripts/install_state.py`: its exact derived description.
- `scripts/llm_wiki/doctor_capture.py`: its exact derived description.
- `scripts/llm_wiki/doctor_lsp.py`: its exact derived description.
- `scripts/llm_wiki/doctor_repair.py`: its exact derived description.
- `scripts/llm_wiki/doctor_schedule.py`: its exact derived description.
- `scripts/llm_wiki/install_inspection.py`: its exact derived description.
- `scripts/llm_wiki/install_release.py`: its exact derived description.
- `scripts/llm_wiki/install_requests.py`: its exact derived description.
- `scripts/llm_wiki/install_resource_builder.py`: its exact derived description.
- `scripts/pyright_profile.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_what_is_not_yet_due_is_not_a_warning.py -q`.
