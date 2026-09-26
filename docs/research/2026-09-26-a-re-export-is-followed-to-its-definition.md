# A re-export is followed to its definition

Date: 2026-09-26. Audit 2026-09-26 B-6.

## Facts

- `from lib import compute as calc` resolved `calc()` to `lib.compute` and looked
  for a definition of `compute` in `lib`. `lib/__init__.py` only has
  `from .core import compute`, so the call became `missing_dependency`, and the live
  `lib.core.compute` looked uncalled to `find_dead_code`.
- The Python language reference (https://docs.python.org/3/reference/simple_stmts.html,
  the `from` form of `import`, fetched 2026-09-26): for each identifier, "check if
  the imported module has an attribute by that name … otherwise, a reference to
  that value is stored in the current namespace". A package's `from .core import
  compute` makes `compute` an attribute of the package: the same object.

## Decision

- The extractor records every module-level `from x import y [as z]` as a
  re-export `(module, z) -> (x, y)` during the definition pass. A symbol with no
  definition in its module follows that chain, at most `MAX_REEXPORT_HOPS` (8)
  modules and never around a cycle, to the first definition.
- `EXTRACTOR_VERSION` is `code-extractor/v15`, so reused generations are rebuilt
  with the new edges instead of keeping the old answer.

## Files

- `scripts/code_extractor.py`
- `tests/test_a_re_export_is_followed_to_its_definition.py`
- `CHANGELOG.md`
