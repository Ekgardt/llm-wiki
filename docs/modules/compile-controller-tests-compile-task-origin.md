# Tests of `scripts/compile_controller.py`

`tests/test_compile_task_origin.py` exercise `scripts/compile_controller.py` through its public names.

`scripts/compile_controller.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_task_origin.py -q`.
