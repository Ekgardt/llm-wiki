# A code answer is shaped throughout its tree

Research and qualification: 2026-10-03. Python 3.10-compatible implementation.

The old answer shaper stopped six transformations at depth six. Its own comment
said the basis was unknown. Below that depth opaque identifiers survived,
repeated module names remained, and the budget ladder could not find row tables.
A forty-row table nested fourteen levels deep consequently returned a refusal
even though two complete cited rows fit the requested budget. Separately,
recursive inspection of nested opaque lists exhausted Python's call stack.

The installed checkout's built-in code generation and navigation were read before
design: commit `28722c39`, generation `generation-18db0357bbb72129-507cd83b`.
Eight shaping symbols were inspected. The graph reported incomplete coverage
and 98,404 unresolved observations; source inspection supplied the ordering and
container details that the graph alone could not prove.

Three independent primary sources were read on the research date:

- [Python 3.10 JSON documentation](https://docs.python.org/3.10/library/json.html):
  dictionary order is retained by default, circular-container checking matters,
  and untrusted JSON still needs resource controls.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html): JSON arrays retain order;
  implementations can impose technical limits. It supplies no product basis for
  stopping only some transformations at six nested containers.
- [MCP tools specification, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools):
  text and structured tool results must retain their declared meaning. It does
  not prescribe a six-level answer-shaping cutoff.

Raising the number would move the same unsupported boundary. Ignoring all deeply
nested values would retain the demonstrated refusal. A new setting would require
an additional contract and would leave correctness dependent on its value. The
chosen change uses explicit stacks for the existing transformations. It preserves
their pre-order or post-order behavior, child order, protected fields, profitable
row constants and prefixes, and the existing dictionary-only columnar pass.
Ancestor tracking rejects cycles while allowing the same acyclic object to occur
on separate branches. No runtime path, environment contract or MCP tool changes.

Six regressions failed against the original implementation. Eight new cases now
cover deep identities, module names, a budgeted cited table, shared acyclic rows,
a 1,500-level opaque collection, cyclic input, recoverable identities and deep
table constants. The related suite passed 66 tests; structure and function-shape
checks passed 52; Ruff passed. Actual Lizard measurements matched AST starting
lines for 45 changed/new callables, with maximum CCN five and the required
conditional/nesting checks passing.

The full useful comparison initialized an actual stdio MCP worker, made three
navigation requests on the installed repository graph, and closed it normally.
Only the shaping module differed. Results were identical apart from generation
time, including citations, budget reports and the graph's incomplete-coverage
warning. Complete cycles took 8.748 seconds before and 9.842 seconds after; these
single observations with a concurrent normal compile are not an acceleration
claim. The worker's existing internal flag was used because supervisor re-exec
would discard an injected candidate module; supervisor behavior is outside this
comparison. No source, provider or successful outcome was fabricated.

The deep-table comparison improved the useful result: the old 138-estimated-token
refusal contained no rows, while the new 427-estimated-token answer retained two
complete file/line rows within the caller's 600-token budget and explicitly
reported 38 omitted rows. These are repository token estimates; neither
navigation nor shaping made model calls. They do not establish improvement in
the separate, still incomplete grounded-answer qualification.

Technical JSON serialization limits remain. The change removes the unexplained
product traversal cutoff, not every implementation or resource bound. It does
not close the entire outstanding audit finding about unsupported limits.

Private evidence remains under ignored logs: `answer-traversal-navigation`,
`answer-traversal-complexity`, `answer-traversal-full-wire-before-worker`,
`answer-traversal-full-wire-candidate-worker`, and
`answer-traversal-paired-proof`, all dated 2026-10-03. Initial supervisor runs
were retained separately and are not used as a candidate comparison.
