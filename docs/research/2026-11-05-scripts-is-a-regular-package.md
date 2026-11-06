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

The name `scripts` itself resolves differently. A namespace package is only a fallback: Python
records the directory and keeps scanning `sys.path`, and a regular package found anywhere later
wins (PEP 420; CPython `PathFinder._get_spec`). With the file, the first `scripts` on the path
wins. That has two effects:

- Another regular package named `scripts` later on the path no longer takes the name. Before, it
  made `import scripts.code_graph` fail with `ModuleNotFoundError`.
  `tests/test_scripts_is_a_regular_package.py` fails without the file because of this.
- Another `scripts` directory without `__init__.py` later on the path no longer adds submodules
  to the name. Nothing in this repository relies on that merge.

The live code graph's Jedi resolution was measured because Jedi 0.20.0 skips parent directories
holding an `__init__.py` when it builds a search path (`Project._get_sys_path`). Real Jedi, with the repository root as
the workspace and through `code_graph._inferred_targets`, resolved the same targets with and
without the file for all 111 calls through plain sibling imports in the 70 smallest modules of
`scripts/` (110 resolved in both runs).

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
- mypy finishes, but it does not see across most of `scripts/`. Modules there import each other
  with plain `import memory_state`, and from the repository root mypy cannot resolve those names.
  Without flags each one is an `import-not-found` finding (772 of the 799 measured name a module
  of `scripts/`); with `--ignore-missing-imports` it becomes `Any`. A call through such an import is therefore not checked, so a broken signature
  in another module of `scripts/` may report nothing.
- `MYPYPATH=scripts` does not close that gap on top of this file: mypy stops with exit code 2,
  "Source file found twice under different module names". Closing it means choosing one import
  style for `scripts/`, which is a separate and much larger change.

## A second stop this file does not clear

The outside check still ends without a verdict after this change, with exit code 2 at a different
place: `numpy/__init__.pyi:737`, "Type statement is only supported in Python 3.12 and greater". The
lock installs numpy 2.5.1 on Python 3.12 and later, and its type stubs use that syntax, so a mypy
that reads them for an older target stops.

Measured with numpy 2.5.1 installed, `--ignore-missing-imports scripts`:

| mypy | Settings | Result |
|---|---|---|
| 2.2.0, 2.0.0, 1.19.1 | `--python-version 3.10` | Exit 2 on the numpy stub |
| 2.4.1 | Any target | Does not stop on the numpy stub |
| 2.2.0 | `--python-version 3.10`, `[mypy-numpy.*] follow_imports = skip` | Exit 2 on the numpy stub |
| 2.2.0 | `--python-version 3.10`, `python_version = 3.12` in the configuration | Exit 2 on the numpy stub: the flag wins |
| 2.2.0 | No flag, `python_version = 3.12` in the configuration | Exit 1, 1 674 findings in 101 files |
| 2.2.0 | `--python-version 3.10`, without this file | Exit 2 on `code_graph.py`, the original stop |

The exit 1 and exit 2 figures in "Alternatives" above were measured on mypy 2.4.1 without numpy, so
they did not see this stop.

A trial commit put `mypy.ini` with `python_version = 3.12` in the merged tree of the outside check.
It stopped on the same numpy line. So that check either passes its own target or does not read this
repository's configuration; which one, its mypy version and its command are not visible from here.
The trial was removed.

No change here fits: raising `requires-python` drops supported Python versions, and holding numpy
back for every Python changes what the product runs on. Both would bend the product around one
check's setup. Using a mypy that does not stop here, or a target of 3.12 or later, is a change to
that check. That is a conclusion from the measurements above, and the owner of the check has to
confirm it.

## A test that depended on the speed of the disk

The first full run of the outside check on this branch blamed
`tests/test_compile_cache.py::test_fd_read_rejects_same_inode_change_between_lstat_and_open`, which
this change does not touch. Its log does not show that failure's details, so the cause below is
the one reproduced here, not one read from that run.

The test rewrites a cache entry in place at the same size and expects the read to notice. The read
compares device, inode, size and mtime, so only the mtime can show this change, and two writes
inside one clock tick share an mtime. On disk the `fsync` in between outlasts the tick and hides
this. Repeated in one process, 3 000 times each:

| Storage | Failures |
|---|---|
| ext4 | 0 |
| ext4, `fsync` removed | 2 545 |
| `/dev/shm` | 2 688 |

Forcing an equal mtime fails the same way with and without `scripts/__init__.py`. A single run in a
fresh pytest process on `/dev/shm` passed, so the rate depends on how warm the process is.

The test now moves the mtime on after its rewrite, and still expects the same `PermissionError`: 0
failures in 3 000 on `/dev/shm`, and it fails if the mtime is removed from the comparison. The
code's limit stays and is now stated: a same-size rewrite inside one tick is not seen by this
comparison, and ctime does not help (measured: 1 373 of 2 000 rewrites kept both stamps).

Later (2026-11-06): that limit was removed. Readers now wait until a file's stamps are a clock
tick old before they trust its stat, and the test again rewrites in place. See
`docs/research/2026-11-06-a-rewrite-in-the-same-tick-is-a-change.md`.

## Sources

- mypy 2.4.1 source, `mypy/find_sources.py`, read from the installed package.
- numpy 2.5.1 stubs, `numpy/__init__.pyi`, read from the installed wheel.
- CPython 3.12 source, `importlib/_bootstrap_external.py`, read from the installed interpreter.
- [PEP 420](https://peps.python.org/pep-0420/), the search order, read through search results
  and [a mirror](https://peps-aaturner.readthedocs.io/pep-0420); the network policy of the
  session blocked a direct fetch of `peps.python.org`, `docs.python.org` and the mypy
  documentation.
- [python/mypy#6682](https://github.com/python/mypy/issues/6682), a report of the same error.
