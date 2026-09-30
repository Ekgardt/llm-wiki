# Argument bindings are stored evidence

Date: 2026-09-30. Status: installed and qualified; generation rebuilt.

The extractor records which caller name reaches which callee parameter.
Its stored BINDS_ARGUMENTS literal is cut after eight pairs or 256 bytes,
with a +N more marker. The source declares no basis for either number.
This is evidence storage, not merely answer display: later reader calls
cannot recover the omitted bindings from the stored literal.

Primary sources checked today:

- [Python 3.10 AST](https://docs.python.org/3.10/library/ast.html): call
  arguments and keyword arguments are finite sequences in the parsed source.
- [SQLite datatypes](https://www.sqlite.org/datatype3.html): TEXT stores
  the literal; no 256-byte field width is imposed by this schema.
- [MCP tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools):
  tool output and its structured representation are distinct from authoritative
  stored input/evidence. This source supplies no eight-binding limit.

Choose complete serialization of the bindings the existing extractor proves.
Keep its refusal of unresolved/ambiguous callees and of arguments that do
not name caller-visible values. Existing source input budgets, cancellation,
extraction deadline, graph validation and answer budgets remain. No new schema,
path, configuration, dependency or service is introduced. Increase the existing
extractor identity so derived generations built with cut bindings are rebuilt.

Alternatives: larger constants preserve eventual evidence loss; new display
settings cannot restore already-discarded facts; reparsing source on every
reader call duplicates extraction and would mix generations. Complete storage
can use more bytes for very wide calls; source budgets and explicit answer
budgets must handle this, rather than permanently deleting known bindings.

Qualification must reproduce a ninth binding and a binding after the former
byte bound, then check exact stored evidence, existing ambiguity/literal
refusals, caller cancellation/deadlines and real complexity guards.

The two real extraction regressions failed before the change. After correcting
a new test's measured CCN without weakening its assertions, the complete related
group passed 133 tests in 37.99 seconds, including actual complexity/branch
guards, extraction deadlines and generation-reuse checks. Ruff passed.
The extractor is now code-extractor/v19, so prior derived generations cannot
reuse the cut bindings as current evidence. The source was installed under
the existing maintenance fence with verified preimage/after hashes and a
retained rollback copy. Replaced constants and the old truncation helper
were removed after checking their only production caller.

Evidence: logs/audit-2026-09-30-finish-bindings-red.txt,
logs/audit-2026-09-30-finish-bindings-final-green.txt and
logs/audit-2026-09-30-finish-bindings-activation.json.

The installed generation was rebuilt as generation-18da2dc5362ba2d2-6d22338d. Doctor reports generation, transactions, queue, scheduler, hooks and MCP as ok; historical capture/tool counters, resource-size advice and retained LSP evidence still make overall health degraded. This is not a clean-health claim.
