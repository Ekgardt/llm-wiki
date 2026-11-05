# `scripts` is a regular package

Date: 2026-11-05. Scope: `scripts/__init__.py` (new and empty). Requested by the repository
owner so that an outside type check of pull requests can finish. The repository itself still
does not run mypy.

## What failed

`mypy scripts` (mypy 2.4.1) stopped with exit code 2 before checking anything:
`scripts/code_graph.py:52`, `:858` and `:942`: "No parent module -- cannot perform relative
import". With no `__init__.py`, mypy names the file as the top-level module `code_graph`, which
has no parent for `from . import ...` to reach (`mypy/find_sources.py`, `crawl_up`). An error at
this stage stops the whole run, so no other finding can be reported.

## How `scripts/` is used, and what the file changes

| Use | Evidence | With the file |
|---|---|---|
| By path: `python scripts/x.py` | `install.sh`, `install.ps1`, hooks, MCP registration | Unchanged: `scripts/` is `sys.path[0]`, the relative import has no parent and raises `ImportError`, and the plain-import fallback runs |
| Plain `import x` | `tests/conftest.py`, `pythonpath = ["scripts"]` | Unchanged: `scripts/` is on the path |
| `import scripts.x` | `tests/test_code_graph.py`, `tests/test_automatic_writer_integration.py` | Same modules; the name becomes a regular package instead of a namespace package |
| `python -m scripts.x` | Not used by the installers or CI | Would keep working |
| Packaging | `uv.lock` has `source = { virtual = "." }`; `pyproject.toml` has no build system | Nothing is built, so nothing new is installed |

One behaviour does change. A namespace package is only a fallback: Python records the directory
and keeps scanning `sys.path`, and a regular package found anywhere later wins (PEP 420; CPython
`PathFinder._get_spec`). So today any other regular package named `scripts` later on the path
takes the name, and `import scripts.code_graph` fails with `ModuleNotFoundError`. With the file,
the first `scripts` on the path wins. That is the only runtime difference, and
`tests/test_scripts_is_a_regular_package.py` fails without the file because of it.

## Alternatives, measured on this repository

| Option | Result | Why (not) |
|---|---|---|
| Empty `scripts/__init__.py` | Exit 1, 1 671 findings in 101 files | Chosen: needs no flag or tool configuration, and states what `from . import` already assumes |
| `--explicit-package-bases` | Exit 1, the same 1 671 findings in 101 files | Works only if every caller passes the flag or reads a configuration added for a tool this repository does not use |
| Remove the relative imports | Not tried | Larger change to working code; `import scripts.x` relies on them |

Both runs used `--ignore-missing-imports`. Exit 1 means mypy finished and reported findings. The
findings are older than this change and are not fixed by it.

## Trade-offs and limits

- The repository's own code index is expected to gain one empty `scripts` module node (read in
  `import_resolver.py`, not measured).
- `mypy .` still stops with exit code 2 on the deliberately broken fixture
  `tests/fixtures/code_kernel/python/pkg/broken.py`. That is a separate matter from this file.
- `import value_references` in the fallback branches names a module mypy cannot resolve from the
  repository root. It counts as an ordinary finding and does not stop the run.

## Sources

- mypy 2.4.1 source, `mypy/find_sources.py`, read from the installed package.
- CPython 3.12 source, `importlib/_bootstrap_external.py`, read from the installed interpreter.
- [PEP 420](https://peps.python.org/pep-0420/), the search order, read through search results
  and [a mirror](https://peps-aaturner.readthedocs.io/pep-0420); the network policy of the
  session blocked a direct fetch of `peps.python.org`, `docs.python.org` and the mypy
  documentation.
- [python/mypy#6682](https://github.com/python/mypy/issues/6682), a report of the same error.
