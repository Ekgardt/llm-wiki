# Validate and execute the Task 27 comparative benchmark contract

`benchmark/run_comparative.py` and the tests that exercise them most.

Ordinary CI uses ``--smoke`` or ``--fixture``. A real run requires an explicit manifest, a successful ``--preflight``, the pinned Graphify checkout, the exact model identity, and complete Gate F evidence. No dependency is downloaded.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
