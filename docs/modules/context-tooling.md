# Maintenance context qualification tooling

The test-only owners `tests/module_context.py` and `tests/module_boundaries.py`
measure declared context and check import boundaries. They do not run in the
product or alter runtime state. The counter resolves contained files/directories,
requires actual pytest bootstrap and recursive literal plugins, checks local
product-source import closure, serializes full files and counts o200k_base tokens.
The user ceiling is100000. Missing context fails before counting; nothing is
silently added. Exact duplicate serialization must round-trip every path/byte.

Source closure conservatively includes lazy/conditional AST imports, relative
modules, imported submodules and package initializers under scripts/. Cycles
terminate through visited paths. Installed dependency code is not vendored into
packets. Dynamic module loading, external processes, config-selected providers,
and cross-module integration inputs still need explicit contracts and evidence;
this static check is not a runtime dependency oracle or a sandbox.

The boundary helper checks static imports and can start a guarded subprocess
with an explicit local-module allowlist. Permitting one dependency does not permit
its entire package or its undeclared imports. The subprocess is a test of import
coupling; it does not replace file/SQL/ownership behavioral tests.

Own tests use synthetic repositories and refusal cases, plus checks of actual
manifests. This packet owns these qualification helpers and their tests. Other
modules include the helper source as required shared context; that overlap is
not unique file ownership and must not be counted as project-size savings.

Run `uv run pytest tests/test_module_context_bootstrap.py
 tests/test_module_context_dependencies.py tests/test_module_boundary_checks.py
 tests/modules/context_tooling -q` as a single command.
Source: ../research/2026-11-03-context-source-closure.md.

Unique ownership is declared separately in each manifest's `ownership` object.
`tests/module_ownership.py` validates claims against the tracked Git index and
measured context, refusing duplicates or missing/untracked/outside claims.
Run `uv run python -m tests.module_ownership` for the full registry, and add
`--check-complete` to require zero unassigned tracked files. The CLI initializes
this repository's existing scripts import path in its own process. Its normal
report can succeed while honestly reporting `complete: false`; the completion
gate returns failure in that case. No file class is silently excluded.
See ../research/2026-11-03-module-file-ownership.md.

Source closure includes imports from test/support Python, package initializers
and relative helpers. Only actual pytest bootstrap remains infrastructure;
explicit test imports cannot hide behind that treatment. Mixed public-facade
compatibility tests remain integration obligations unless their full consumer
source is included. See the test-import-context-closure research.
