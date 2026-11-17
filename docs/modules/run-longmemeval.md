# MEM-10: the vault's first honest number on LongMemEval

`benchmark/run_longmemeval.py` and the tests that exercise them most.

Runs the `longmemeval_s` questions against the product's real pipeline: one disposable adopted vault per question (see `longmemeval_vault.py`), capture's own write path for every haystack session, one immutable generation, then `retrieve_via_search_memory` + `grounded_qa` with the configured provider.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
