# Tests of `scripts/install_control.py`

`tests/test_a_changed_task_setting_reaches_an_installed_machine.py` exercise `scripts/install_control.py` through its public names.

`scripts/install_control.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_changed_task_setting_reaches_an_installed_machine.py -q`.
