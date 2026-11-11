# Beginning and executing the whole compile work

`scripts/llm_wiki/compile_controller_work.py` and the tests that exercise them most.

It is one concern part of `scripts/compile_controller.py`; callers and tests import
and patch this part where a name is looked up, not the facade.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_work_completion.py -q`.
