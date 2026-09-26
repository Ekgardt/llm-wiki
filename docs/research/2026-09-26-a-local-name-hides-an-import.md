# A local name hides an import

Date: 2026-09-26. Audit 2026-09-26, finding C-9 ("local assignment does not shadow
an imported name -> false caller").

## What was wrong

Fact, `scripts/code_extractor.py` at a2965838: `_Collector._resolve_name` looked a
called name up in the file's import aliases first, whatever the calling function
had bound. `from lib import compute` followed by `def run(): compute = len;
compute()` recorded a CALLS edge from `run` to `lib.compute` — a caller that does
not exist. The same held for a parameter, a loop target, `with ... as`,
`except ... as`, a nested `def`/`class`, a walrus, a capture pattern, and a name
bound in an enclosing function (a closure). Measured: 12 of the 14 first cases in
the new test fail on the old code.

## Decision

- Python's own rule: a name bound anywhere in a block is local to the whole block
  and to the blocks nested in it, unless declared `global`/`nonlocal`. Before a
  call is resolved, the imports it can see are the file's aliases minus the names
  its function, or a function around it, binds (`_visible_aliases`,
  `_scope_chain_bindings`); a `global` declaration in the calling function gives
  the import back.
- Every binding construct the reference lists is recognised: parameters,
  definitions, assignment and walrus targets, `for`, `with`, `except`, capture
  patterns (`MatchAs`, `MatchStar`, `MatchMapping.rest`), `del`, and type
  parameters where the running Python has them. Import statements inside a
  function stay aliases (they are what the call reaches).
- `EXTRACTOR_VERSION` is `code-extractor/v16`, so stored generations are rebuilt.
- Measured on this repository's `scripts/` (180 files): extraction 12.5 s against
  12.9 s before (best of three), 16 fewer CALLS edges.
- Guard: `tests/test_a_local_name_hides_an_import.py` has one case per binding
  construct of the reference's list, plus closure, `global`, and the untouched
  import.

Not covered: module-level rebinding after the import (`compute = other` at module
level) — that is flow-dependent, and the graph is not flow-sensitive.

## Source

Python Language Reference, 4.2.1 "Binding of names", fetched 2026-09-26 from
https://docs.python.org/3/reference/executionmodel.html:

- "The following constructs bind names: formal parameters to functions, class
  definitions, function definitions, assignment expressions, targets that are
  identifiers if occurring in an assignment: for loop header, after as in a with
  statement, except clause, except* clause, or in the as-pattern in structural
  pattern matching, in a capture pattern in structural pattern matching, import
  statements, type statements, type parameter lists."
- "A target occurring in a del statement is also considered bound for this purpose"
- "If a name is bound in a block, it is a local variable of that block, unless
  declared as nonlocal or global."
- "If the definition occurs in a function block, the scope extends to any blocks
  contained within the defining one, unless a contained block introduces a
  different binding for the name."

## Files

- `scripts/code_extractor.py`
- `tests/test_a_local_name_hides_an_import.py`
