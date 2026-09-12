# The fixtures themselves under the law

Date: 2026-09-12. Trigger: after the navigation and benchmark tests came under
the limit, the last 19 functions over CCN 5 in `tests/` were not tests at all.
They were the fixtures and guards the tests lean on: the fake LSP server
(`_run_process_server` CCN 61, `_run_semantic_server` CCN 48), the AST guard
that proves no code deletes the runtime root (`_path_parts` CCN 30), and the
node-probe and pyright-config tests.

## Sources

1. Law 5, verbatim: «при появлении более двух if внутреннюю логику необходимо
   выделять в независимые подфункции», Early Return instead of if-else-if, and
   «сложную логику необходимо разбивать на Pipeline из простых шагов».
2. `~/.claude/tools/ccn/bin/lizard` 1.23.0 — the measurement of record on this
   machine. A function of 300 lines whose body is one `while True` with
   twenty `if` branches measures 61, and that number is the debt.
3. `docs/research/2026-09-12-navigation-tests-under-the-complexity-law.md`:
   the same remedy applied one layer up — a ladder over case names becomes a
   table, and compound assertions become one comparison of named facts.
4. The fixture's own contract: `tests/test_lsp_process.py`,
   `tests/test_lsp_protocol.py` and `tests/code_kernel_helpers.py` drive
   `tests/fake_lsp_server.py` as a subprocess through its flags, so behaviour
   had to stay identical flag for flag.

## Decision

Behaviour unchanged, proven by the same tests passing before and after.

- `tests/fake_lsp_server.py`: the two servers become `_SemanticServer` and
  `_LifecycleServer`, one method per thing the protocol asks of them, each
  returning `done` or `stop` instead of `continue`/`return` inside one loop.
  The canned answers move into `_semantic_default_results`, the connection
  scenarios into `_CONNECTION_SCENARIOS`, and the marker files behind
  crash-once and hang-once into one `_claim_marker`.
- `tests/test_runtime_deletion_contract.py`: the path resolver becomes a
  dispatch on node type (`_name_parts`, `_constant_parts`, `_divided_parts`,
  `_parent_parts`, `_call_parts`), the delete-API recogniser a table, and the
  installer guard a module-level pipeline of `_logical_lines` →
  `_deletes_runtime_root`.
- `tests/test_pyright_profile.py`: the `extends` cases become a table of
  writers, and the node-probe assertions one comparison against a set of
  facts; the fake node binary is installed by one shared helper.

- `benchmark/fixtures/comparative_adapter_fixture.py`: the adapter fixture's
  `main` splits into the probe, the one transient failure, and the success
  payload; its bytes on stdout are unchanged.

The managed gate checks a second thing lizard does not: more than two `if`
statements at one level. Every helper written here was re-measured with
`structure_check.py` and split until none has three.

Files: `tests/fake_lsp_server.py`, `tests/test_runtime_deletion_contract.py`,
`tests/test_pyright_profile.py`, `tests/test_code_navigation_benchmark.py`,
`benchmark/fixtures/comparative_adapter_fixture.py`, `CHANGELOG.md`.
