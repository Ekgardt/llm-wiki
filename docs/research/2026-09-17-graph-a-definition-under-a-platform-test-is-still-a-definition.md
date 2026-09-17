# A definition under a platform test is still a definition

Date: 2026-09-17. Audit 3, code intelligence, finding B23.

Files: `scripts/code_extractor.py`, `tests/test_a_definition_under_a_platform_test_is_still_a_definition.py`

## What was found (verified by running it)

`CodeExtractor._walk_python` (`scripts/code_extractor.py:1196`) iterates the
statement list it is given and nothing else:

```
for node in body:
    self.check_stop()
    self._python_definition(ctx, node, owner)
```

A `def`, a `class` or an UPPER_CASE assignment that sits inside an `if`, an
`else`, a `try`, an `except`, a `finally`, a `with`, a `for`, a `while` or a
`match` case therefore produces no node at all. Reproduced on a module of the
shape this repository writes constantly — one implementation per platform
behind `if sys.platform == "win32": ... else: ...` — the extractor emitted the
module and nothing else, and every call to those functions was recorded as an
`unresolved_reference` observation instead of a `CALLS` assertion.

The shape is not exotic here. `scripts/lsp_process.py`, `scripts/lsp_paths.py`
and `scripts/pyright_profile.py` all define platform-conditional helpers, and
`try: import X except ImportError:` fallback definitions appear across
`scripts/`.

## Sources

- The Python Language Reference, "Execution model" →
  [Naming and binding](https://docs.python.org/3/reference/executionmodel.html#naming-and-binding)
  (fetched 2026-09-17): "The following constructs bind names: formal parameters
  to functions, class definitions, function definitions, assignment
  expressions, …" and "If a name is bound in a block, it is a local variable of
  that block, unless declared as nonlocal or global." The *block* is the
  module, the class body or the function body — the reference lists exactly
  those: "A block is a piece of Python program text that is executed as a unit.
  The following are blocks: a module, a function body, and a class definition."
  An `if` or a `try` statement is not a block, so a
  `def` inside one binds its name in the enclosing scope. The owner of such a
  definition is therefore the same owner the enclosing statement has, which is
  what the walk must record.
- `ast` module documentation (https://docs.python.org/3/library/ast.html,
  fetched 2026-09-17) for the field names a compound statement carries:
  `If`/`For`/`While`/`With`/`AsyncWith`/`AsyncFor` have `body` and `orelse`,
  `Try`/`TryStar` add `handlers` and `finalbody`, `Match` carries `cases`
  (`match_case` objects, each with its own `body`). Those five field names are
  the whole of the descent.

## Decision

`_walk_python` keeps one explicit stack instead of recursing per statement, and
pushes the nested statement lists of any statement that defines nothing. The
walk stays in source order (pre-order: the stack is extended with the nested
lists reversed), so node ids and definition lists are produced in exactly the
order they were before for code that has no nested definitions.

- The stack is iterative, so a deeply nested module cannot raise
  `RecursionError` out of the extractor regardless of how deep `ast.parse`
  accepted it.
- Descent stops at anything that defines something: a `ClassDef` or a
  `FunctionDef` is handled by its own writer, which recurses with the new owner
  it establishes. Only non-defining compound statements are flattened, and they
  keep the owner they were found under — which is what the language reference
  requires.
- `_python_constant` still admits only module-level UPPER_CASE names; a
  constant behind `if sys.platform == …` is module level and is now recorded,
  which is the point.
