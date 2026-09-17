# Reserved names are a Windows question

Date: 2026-09-17. Audit 3, code intelligence, finding A1 (K-1).

Files: `scripts/pyright_profile.py`, `tests/test_reserved_names_are_asked_only_on_windows.py`

## What was found

`_path_is_reserved` prefers `os.path.isreserved` and otherwise calls
`Path.is_reserved()`. `os.path.isreserved` exists only in `ntpath`, so on Linux
and macOS every Python version takes the second branch. Measured on CPython
3.14.7 with `-W error`: `hasattr(posixpath, "isreserved")` is `False`,
`hasattr(ntpath, "isreserved")` is `True`, and `Path("/x").is_reserved()` raises

> DeprecationWarning: pathlib.PurePath.is_reserved() is deprecated and scheduled
> for removal in Python 3.15. Use os.path.isreserved() to detect reserved paths
> on Windows.

`PureWindowsPath.is_reserved()` raises the same warning. `pyproject.toml`
declares `requires-python >=3.10` with no upper bound, so on 3.15 the call
becomes an `AttributeError` inside `_is_local_absolute_path`, and every Pyright
discovery on POSIX fails. It is the only use of `is_reserved` in the repository.

## Sources

- Python 3.14 `pathlib` documentation, `PurePath.is_reserved()`
  (https://docs.python.org/3.14/library/pathlib.html, fetched 2026-09-17):
  "Deprecated since version 3.13, will be removed in version 3.15: This method
  is deprecated; use os.path.isreserved() to detect reserved paths on Windows."
- Python 3.14 `os.path` documentation, `os.path.isreserved(path)`
  (https://docs.python.org/3.14/library/os.path.html, fetched 2026-09-17):
  "Return True if path is a reserved pathname on the current system." —
  "Availability: Windows." — "Added in version 3.13."

## Alternatives

1. Pin `requires-python <3.15`. Hides the defect and blocks the interpreter the
   owner will install next.
2. Suppress the warning. The method is removed, not just noisy.
3. Ask the question only where it has an answer. On POSIX no name is reserved,
   and `PurePosixPath.is_reserved()` has always returned `False`, so answering
   `False` without a call keeps today's behaviour. On Windows use
   `ntpath.isreserved` when the interpreter has it (3.13+) and
   `PureWindowsPath.is_reserved()` only below that, where it does not warn.

## Decision

Alternative 3. The Windows half is a pure function of the text, so it is tested
on every platform; the POSIX half is tested with a path object that has no
`is_reserved`, which is what 3.15 will hand us.
