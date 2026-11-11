# Maintenance context must retain local source dependencies

2026-11-03; approved decomposition-completion mandate; baseline c8036cde.
The ad-hoc AST audit of all44 current packets found no omitted statically visible
local source dependency. However the common measurement helper enforces pytest
bootstrap and the codec, but has no regression guard against a future local
source import being omitted from a manifest. Add that check before token counting
and qualify the test-only maintenance-context tooling with its own complete
packet. This adds no runtime behavior, dependency or limit.

Research checked today across three independent primary publishers:
- Python import reference documents absolute/relative imports, package initializer
  execution and dynamic-import limits:
  https://docs.python.org/3.10/reference/import.html
- Pytest documents implicit conftest and recursive pytest_plugins discovery:
  https://docs.pytest.org/en/stable/how-to/writing_plugins.html
- Import Linter documents precise forbidden/protected/layer/independence contracts:
  https://import-linter.readthedocs.io/en/stable/contract_types/
The earlier .html spelling of the last URL returned404; the canonical page was
read successfully. Python3.10 compatibility remains; no tool is newly installed.

Choose a small static check in tests/module_context.py, consuming the same
resolved declared paths as the existing counter. For every declared product
Python source under scripts/, resolve existing local absolute/relative module
imports, submodule imports and package initializers. Include lazy/conditional
imports conservatively. Require the complete recursively reachable local source
closure in the declaration; name missing paths and fail rather than silently
adding them. Existing containment checks apply to dependency paths too.

Installed dependencies are represented by the existing package contract, not
copied into each packet. Arbitrary dynamic Python loading, configuration-selected
providers, subprocess commands and actual behavioral isolation still require
source research/integration tests; static closure is not a claim to solve those.
Test files are not blindly followed into every integration bootstrap import:
that would pull the whole product into every own unit packet and contradict the
approved explicit bootstrap/integration distinction. Required test helpers,
fixtures and integration ownership remain separately tracked obligations.

Alternatives: continue hand-maintained dependency review alone (no regression
protection), import/execute every product module (side effects and platform
requirements), install a graph dependency (unneeded for this bounded file check),
or silently append missing context (hides a broken declaration). Reject these.
Before implementing, reproduce an underspecified synthetic packet that currently
passes; add failing guards for direct, transitive, lazy, relative and package
imports, cyclic imports, and outside-root dependencies. Preserve the100000-token
ceiling, o200k encoding, exact-duplicate serialization and all existing checks.

Reproduction before the guard: a synthetic packet declared owner.py but omitted
its existing imported dependency.py and was accepted at125 tokens. The first
new tests failed13 cases and passed1; implementation then passed82 related cases.
A further test exposed implicit initializer loading for a declared source even
when that source has no imports. That test failed before the correction; the
corrected guard then named a real omission in context-budget-context.json.
Review found the same missing scripts/llm_wiki/__init__.py in lease-renewal and
process-liveness packets. Its only content is the package docstring, but it is
still required context. All three manifests now declare it. The earlier ad-hoc
explicit-import audit was insufficient for this implicit-load case; do not cite
its clean result as proof that these historical manifests were complete.

Final checks: weighted helper/bootstrap/own packet run75 passed; Python3.10 run
including tiktoken measurement85 passed; all module/bootstrap/dependency/boundary,
complexity/structure/CI/privacy/limit/weight guards997 passed/2 skipped. Two new
resolver helpers initially measured CCN6; separating existing-path selection
reduced every changed function to <=5 without weakening behavior. Ruff passes.
All45 emitted artifacts round-trip every declared path and byte and meet the
unchanged token ceiling. The new context-tooling packet owns the measurement
and boundary helpers/tests; it does not claim whole-project ownership completion.
The Python3.10 temporary test venv now has the project's existing tiktoken0.14.0
installed for real measurements; no project dependency or live environment changed.
