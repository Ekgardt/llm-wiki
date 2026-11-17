# Tests filed with `scripts/install_control.py`

- `tests/test_a_scheduled_run_keeps_the_uv_link.py`: A scheduled run calls uv by the path the operator's PATH gives, links kept.

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/installer_config.py`: its exact derived description.
- `scripts/llm_wiki/doctor_schedule.py`: its exact derived description.
- `scripts/llm_wiki/install_scheduler_render.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_scheduled_run_keeps_the_uv_link.py -q`.
