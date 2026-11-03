# Trusted execution-profile conversion

The existing `scripts/trace_collect.py` stays byte-identical at its public path.
It converts an operator's own trusted cProfile profile into `execution-trace/v1`
JSON lines. It neither opens a graph nor binds frames to generations, updates
runtime state or establishes general reachability. Trace ingestion and dynamic
edge publication remain separate responsibilities.

## Input and output contract

Profiles are trusted input only: pstats reads a marshal-based format. This owner
is not a safe loader for hostile or unauthenticated profiles. Use a profile just
produced by the same supported Python runtime. The existing CLI accepts profile,
destination and optional repository root; no wrapper or new option is introduced.

The converter resolves the root and frame paths, excludes synthetic/outside-root
frames, and retains profiler names, first line numbers and existing call-count
interpretation. Decorated function keys can name the first decorator line; the
collector does not rewrite that to a definition line. Ingestion owns the separate
decorator-window binding rule. Zero/negative counts are omitted by the existing
edge builder. Profile and filesystem errors keep their existing behavior.

Output has one header followed by JSON edge objects and a final newline. The
header includes the profile basename, resolved repository root and edge count.
Frame paths are repository-relative. Conversion retains the full eligible list
in memory; this qualification does not invent a streaming or memory-bound promise.
Writing replaces the caller-selected destination and creates no parent directory.

## Qualification — 2026-11-03

The approved autonomous decomposition plan permits existing cohesive owners to
stay in place. Combining trusted profile loading with untrusted trace ingestion
would lose the existing security separation. A replacement collector, protocol
wrapper or new runtime store is unnecessary for ownership qualification.

Current sources checked:
[Python 3.14 profilers](https://docs.python.org/3.14/library/profile.html) and
[marshal security contract](https://docs.python.org/3.14/library/marshal.html),
plus independent boundary guidance from
[Fowler](https://martinfowler.com/bliki/BoundedContext.html),
[Import Linter](https://import-linter.readthedocs.io/en/stable/contract_types/)
and [PEP 544](https://peps.python.org/pep-0544/). Standard-library dependencies
remain unchanged. No algorithm or profiler replacement is claimed as necessary.

The complete August28 trace design and August29 frame-key correction remain
context dependencies covering the wider trace subsystem, whose historical owner
is still unfinished. Their old runtime-floor rationale and measurements remain
dated evidence; today's Python 3.14 floor is stated in the global runtime contract.
Alternative collectors now available do not justify changing this converter's
behavior during decomposition.

The baseline real profile yielded two edges, including two calls from caller to
leaf. Owner tests use actual cProfile output, decorated frame identity, external
frame exclusion, the real CLI and failure on a missing trusted profile. Existing
trace-ingestion regressions remain intact. Import guards prohibit upward product
dependencies. Full instructions, implementation, tests and historical documents
must fit 100000 tokens and reconstruct exactly. No live installation or paired
LLM task-cost reduction is implied.

CI run 41691722619 found that the new profile fixture read its UTF-8 source
without declaring the encoding. The existing repository encoding guard reproduced
the failure locally. The fixture now uses the same explicit UTF-8 encoding for
writing and reading; the guard and production converter remain unchanged.


## Preserved structure reference — 2026-11-03

The converter retains trusted-profile input; graph ingestion and dynamic-evidence
publication remain separate responsibilities.
