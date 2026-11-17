# Every re-export form is followed

Update 2026-09-30: the eight-hop bound described below is superseded by
`2026-09-30-traversal-stops-at-the-budget.md`; cycles and caller deadlines remain
enforced. The historical measurements are retained.

Date: 2026-09-26 (audit of 2026-09-27, item C-7).

## What was true

Reproduced on 2026-09-26 with `extract_code` on three three-file packages, each
with `app.py` calling `compute` imported from `lib`:

- `lib/__init__.py`: `from .core import *` -- no CALLS edge (the star was recorded
  as a re-export of a name `*`).
- `try: from .fast import compute / except ImportError: from .slow import compute`
  -- `missing_dependency`: only statements directly in the module body were read.
- `from .old import compute` then `from .new import compute` -- the edge went to
  `lib/old.py`: `setdefault` kept the first binding.

## Sources

1. Python language reference, the import statement
   (https://docs.python.org/3/reference/simple_stmts.html, read 2026-09-26): "If
   the list of identifiers is replaced by a star ('*'), all public names defined in
   the module are bound in the local namespace"; "The public names defined by a
   module are determined by checking the module's namespace for a variable named
   `__all__` ... If `__all__` is not defined, the set of public names includes all
   names found in the module's namespace which do not begin with an underscore
   character ('_')."
2. Python language reference, execution model
   (https://docs.python.org/3/reference/executionmodel.html, read 2026-09-26):
   import statements are among the constructs that bind names, and "Name
   resolution of free variables occurs at runtime, not at compile time" -- the
   binding in force is the latest one executed.
3. Pyright, "Type Concepts - Advanced" (docs/type-concepts-advanced.md, main,
   read 2026-09-26): "Type narrowing is applied whenever a symbol is assigned a
   new value", and after a conditional block "the narrowed type of `val` becomes
   `int | str` because the type checker cannot statically predict whether the
   conditional block will be executed at runtime." -- a straight-line binding
   replaces, a branch adds an alternative.

## Alternatives

- Resolve a fallback import to its first branch: the extractor would claim a
  single callee it cannot prove, and the other implementation would look dead.
- Evaluate `__all__` beyond a literal list or tuple (`+=`, comprehensions): needs
  execution; a module whose `__all__` is not a readable literal is treated as
  having none, so its public names are exported.
- Chosen: record every module-level `from x import ...`, including those inside
  `if`, `try` and `with` blocks; a straight-line import replaces the earlier
  bindings of that name and one in a block adds an alternative; a star import
  hands on what the source's literal `__all__` names, else its public names.
  Several alternatives follow the extractor's existing rule for several
  definitions: no CALLS edge, an `ambiguous_target` observation naming the
  candidates, so neither implementation looks dead.

## Consequences

- `EXTRACTOR_VERSION` is `code-extractor/v17`, so reused generations are rebuilt.
- `MAX_REEXPORT_HOPS` (8, from B-6) now bounds the hops visited across all
  alternatives, not along one chain.
- An `__all__` extended with `+=` is read as its first literal only, so a name
  added later is not handed on by a star import (a missed edge, never a wrong one).
- Guard: `tests/test_every_re_export_form_is_followed.py`; three of its five
  cases fail on the old extractor.
