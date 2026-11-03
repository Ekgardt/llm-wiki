# Python lexical and workspace import resolution

`scripts/import_resolver.py` remains byte-identical at its public import path.
It depends only on the standard library and the complete `python_parse` module.
It builds frozen symbol/module sets and resolves source imports and calls using
lexical bindings plus optional workspace evidence. It never imports inspected
modules, publishes a graph, starts a language server or writes runtime state.

## Existing contract and limits

Registry construction prunes hidden directories and named workspace caches below
the selected root; a hidden root itself remains usable. File selection retains
regular files within the existing 8 MiB bound using lstat. This precheck does not
claim race-proof containment against a hostile concurrently changing tree.
The walk keeps the selected path list and extracted records; parsed trees are
released per file. Re-exports resolve to a monotone fixed point over records.

Optional monotonic deadline and cancellation callbacks stop the directory walk,
per-file extraction and re-export rounds with TimeoutError. They are cooperative
checks, not an interruption guarantee inside an OS read or individual parse.
The default unbounded call remains supported. Existing parse/read failures and
visitor recursion failures produce the same skipped/empty results as before.

Call records preserve names, source positions, confidence, evidence, semantic
eligibility and unresolved reason. Lexical imports, local definitions, methods,
shadowing and branch scopes retain their current behavior. Workspace symbols
support confirmation of import targets; unknown or dynamic targets are not
invented as confirmed edges. This is not a full Python runtime or type checker.
The graph caller owns how these records enter a user answer or generation.

## Qualification — 2026-11-03

The approved autonomous plan permits this existing boundary to stay in place.
Merging it with graph orchestration would expand its responsibility. Replacing it
with LSP would change fallback behavior and lifecycle. Neither is needed here.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[PEP 544](https://peps.python.org/pep-0544/),
[Python name resolution](https://docs.python.org/3.14/reference/executionmodel.html),
[AST](https://docs.python.org/3.14/library/ast.html),
[filesystem walking](https://docs.python.org/3.14/library/os.html) and
[static data-flow limits](https://codeql.github.com/docs/writing-codeql-queries/about-data-flow-analysis/).
No analyzer dependency is added; these references do not establish complete
semantic resolution or a new containment guarantee.

The existing cancellation, allocation and registry-walk files and subprocess
probe remain unchanged and are fully owned here. Existing graph, deep-parse and
live-file integration checks remain unchanged with their consumers. New owner
checks exercise real source files, missing/confirmed import evidence and parser
isolation. The August 29 registry analysis and September file-selection/parse
records remain complete historical context; their dated product status is not
silently rewritten as current status. Full source, tests, helper, dependency,
instructions and history must fit 100000 tokens and reconstruct exactly.
No live installation or paired LLM task-cost reduction is claimed.


## Preserved structure reference — 2026-11-03

Lexical/workspace resolution retains its parser dependency; generation publication
and live language-server navigation remain separate.
