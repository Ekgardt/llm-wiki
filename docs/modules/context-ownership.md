# File and record ownership inventory

`tests/module_ownership.py` owns the read-only Git inventory, unique file-owner
registry and explicit complete-project gate. It expands declared ownership through
[context tooling](context-tooling.md) and composes the
[record-ownership checks](context-records.md). The complete dependency sources are
included. This existing implementation and tests/test_module_ownership.py remain
byte-identical in their original paths.

Git's cached NUL-separated file list is the inventory, so names with spaces remain
intact and untracked files cannot masquerade as owned. Subdirectory roots are
refused. Each manifest must own itself, and each owned file must be tracked and
inside its measured context. Duplicate claims fail. Shared context is not ownership.
The report exposes unassigned files. Normal reporting can successfully return
complete:false; --check-complete must return failure until the project is complete.
No file class or unbounded common bucket is exempted.

Synthetic Git/CLI tests qualify these rules independently. Whole-repository
assertions remain in tests/test_module_registry_integration.py, a visible separate
integration obligation. No implementation, CLI behavior, tests or historical
research were removed by this ownership reassignment.

Sources: [original inventory research](../research/2026-11-03-module-file-ownership.md),
[in-place qualification](../research/2026-11-03-context-ownership-owner.md).
