# Tests filed with `scripts/install_control.py`

- `tests/test_every_scheduler_gets_the_providers_path.py`: A scheduled run on macOS or under cron sees the provider CLIs beside uv, as systemd does.

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/installer_config.py`: its exact derived description.
- `scripts/llm_wiki/install_scheduler_render.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_scheduler_gets_the_providers_path.py -q`.
