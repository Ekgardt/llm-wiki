# MEM-14: does this vault's supersession pick the current value without an LLM?

`benchmark/run_conflict_resolution.py` and the tests that exercise them most.

The claim under test is narrow and it is the whole point: **no LLM takes part in conflict resolution**. The stand proves or refutes it by holding the candidate set fixed and varying only who resolves it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
