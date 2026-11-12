# Tests of `scripts/compile_progress.py`

- `tests/compile_call_controller_helpers.py`: Helpers shared by test_compile_call_controller.py and the test files importing them.
- `tests/compile_progress_admission_helpers.py`: Helpers shared by test_compile_progress_admission.py and the test files importing them.
- `tests/test_compile_progress_admission.py`: New queue readers require coherent adoption evidence, not just a version stamp.
- `tests/test_compile_progress_health.py`: Full health and backup certification include intermediate compile evidence.
- `tests/test_compile_progress_queue_fencing.py`: Checkpoints use the existing queue's lease and payload boundary.

`scripts/compile_progress.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/compile_call_controller_helpers.py tests/compile_progress_admission_helpers.py tests/test_compile_progress_admission.py tests/test_compile_progress_health.py tests/test_compile_progress_queue_fencing.py -q`.
