# Tests of `scripts/install_control.py`

- `tests/test_every_scheduler_gets_the_providers_path.py`: A scheduled run on macOS or under cron sees the provider CLIs beside uv, as systemd does.

`scripts/install_control.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_every_scheduler_gets_the_providers_path.py -q`.
