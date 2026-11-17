# The root plugin carries only the shared fixtures

Date: 2026-11-10.

## Problem

`tests/conftest.py` loaded `tests.code_kernel_helpers` as its only plugin, because three
fixtures every part of the suite asks for by name — `state_root`, `repository` and
`catalog` — lived at its end. Pytest loads a root plugin for every test, so the module
packet check (`tests/module_context.py::required_bootstrap`) correctly put the whole
helper (4 364 tokens) into every packet. The helper also replaces
`pyright_profile.PYRIGHT_PACKAGE_SHA256` and `PYRIGHT_PACKAGE_INTEGRITY` for its Pyright
install fixtures. A patch of a constant depends on the provider's internals, so the
packing tools could not describe `pyright_profile.py` and carried its full source
(16 482 tokens) into packets that never touch Pyright. Four test packets were 174 to
1 010 tokens over the 100 000-token limit, and many script packets were refused as too
large.

## Decision

The three fixtures and the repository builder they need (`copy_python_fixture`,
`create_python_repository`, `FIXTURE_ROOT`) move to `tests/shared_fixtures.py`, owned by
the test-support module. The root conftest loads that file instead. `code_kernel_helpers`
keeps its Pyright and corpus helpers and re-exports the moved names with redundant
aliases, so its existing importers keep working. A test that patches `FIXTURE_ROOT`
now patches it where `copy_python_fixture` looks it up.
`tests/test_code_kernel_helpers.py` asserts that the shared plugin is loaded and the
code-kernel helper is not.

## Sources

- pytest, [Writing plugins](https://docs.pytest.org/en/stable/how-to/writing_plugins.html):
  a conftest loads every module named in `pytest_plugins` eagerly, and requiring plugins
  that way from a non-root conftest is deprecated. A root plugin is therefore loaded for
  the whole suite.
- pytest, [Fixtures reference](https://docs.pytest.org/en/stable/reference/fixtures.html):
  a fixture is available to the tests inside the scope it is defined in, and plugins come
  last in the lookup order, so a test module's own definition still wins.
- Stack Overflow, [Using conftest.py vs importing fixtures from dedicated modules](https://stackoverflow.com/questions/73191533/using-conftest-py-vs-importing-fixtures-from-dedicate-modules):
  shared, general fixtures go in the shared location, and fixtures one area needs stay
  with that area.

## Alternatives considered

- Each of the roughly 110 test files that asks for one of these fixtures imports it
  explicitly, and the root plugin goes away. Rejected: the change is large, and a missed
  file fails only when its test runs.
- Raise the limit or exclude the helper from measurement. Rejected: the limit has no
  exemptions, and the helper really is loaded for every test.

## Trade-offs

Every packet still carries `tests/shared_fixtures.py` (about 700 tokens), because pytest
really loads it for every test. Its own imports (`reliable_memory`, `repository_scope`)
are bootstrap imports, which the measurement does not follow; that is unchanged.
