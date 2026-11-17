# The import graph has no cycle

Date: 2026-11-09.

## What changed

One import cycle joined 77 scripts, most of it through imports written inside
functions. Editing any of them meant reading all of them, so none could become a
module of bounded size. The cycle is gone: shared code moved into modules that
sit below their former callers, and a documented command keeps its file name as
a thin top over a library (`compile_memory.py` over `compile_pipeline.py`,
`repository_index.py` over `repository_indexing.py`, `memory_queue.py` over
`queue_core.py`).

`tests/test_the_import_graph_stays_acyclic.py` holds the line. It counts imports
written inside functions, and it refuses a copied `from x import y` of a global
that module `x` rebinds, because such a copy silently goes stale.

## Alternatives

| Option | Verdict |
|---|---|
| Move shared code below both callers | Chosen. It removes the cycle instead of hiding it. |
| Keep the cycle with imports inside functions | Rejected. It only postpones the failure, and the cycle stays in what a reader must load. |
| PEP 810 `lazy import` | Not available on this project's Python 3.14; the feature targets 3.15. The cycle would also remain. |
| `import-linter` layers contract | Rejected for now. A declared layer order is more than this rule needs, and it adds a dependency; `graphlib` from the standard library finds a cycle. |

## Grounds

Primary sources, read in full on 2026-11-10 (the first version of this note, dated
2026-11-09, relied on search-result summaries; the claims below replace it):

- [PEP 810](https://peps.python.org/pep-0810/), header: "Status: Final",
  "Python-Version: 3.15". FAQ "Do lazy imports work with circular imports?":
  "Lazy imports don't automatically solve circular import problems", a lazy import
  helps "only if the circular reference isn't accessed during module
  initialization", and "The best practice is still to avoid circular imports in
  your code design." FAQ on older versions: `__lazy_modules__` is ignored before
  3.15. So `lazy import` is not available on this project's Python 3.14, and it
  would not remove the cycle.
- [Import Linter, layers contract](https://import-linter.readthedocs.io/en/stable/contract_types/layers/),
  section "Layers": "higher layers may depend on lower layers, but not the other
  way around", including "indirect imports (i.e. chains of imports via other
  modules)". It is configured as an ordered `layers` list, and its install page
  says to install `import-linter` as a separate (dev) dependency. The split follows
  the same rule; enforcing it with the tool would add that dependency and a
  declared layer order for every script, which an acyclicity check does not need.
- [graphlib](https://docs.python.org/3/library/graphlib.html), standard library:
  run on this project's interpreter (3.14.7),
  `TopologicalSorter({'a': {'b'}, 'b': {'a'}}).static_order()` raises
  `CycleError('nodes are in a cycle', ['a', 'b', 'a'])`, so a cycle check needs no
  new dependency.

The Real Python and DataCamp tutorials cited earlier were read only as search
summaries and are no longer used as grounds.

## Trade-offs

A name imported with `from x import y` is a second binding, so a test that patches
`x.y` does not reach a caller holding its own copy. The split moved many names, and
every such test had to name the module that holds the binding the code actually
calls. `tests/compile_paths.py` patches a path on every compile module that holds it.

The split removes the cycle; it does not make every script fit a 100,000-token
packet. A composition root such as `mcp_server.py` still reaches most of the source.
