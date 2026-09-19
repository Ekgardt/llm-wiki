# An unreadable generation must not fail an answer that did not need it

Date: 2026-09-18. The MCP-side consequence of audit 3, B26, plus the tail of
B3 (`_repository_freshness` had no deadline).

Files: `scripts/mcp_server.py` (two functions only),
`tests/test_an_unreadable_generation_degrades_the_decoration.py`

## What was found

`code_graph._active_evidence_graph` used to answer `None` for three different
things, and commit a10f511 separated them: `None` now means only "this
repository has no generation", while an unreadable catalog or database raises
`code_graph.GenerationUnreadable`. That is right at the source — but
`scripts/mcp_server.py` calls it in two places that both read `None` as "no
generation" and degrade quietly, and the tool-call boundary turns any escaping
exception into an error payload:

1. `_repository_freshness` (line 1989) decorates *every* structural answer with
   the generation's commit against the checkout's. It is a decoration: the
   answer itself was already computed. Letting it raise would turn a good
   `get_architecture` into an error because a file the answer never used is
   damaged. It also opened the generation with no deadline at all, while the
   sibling `_freshness_fields` path bounds its own worktree probe at 5 s.
2. `_open_navigation_graph` (line 2340) already returns `None` on a missing or
   mismatched generation, and each of its three callers degrades to structural
   or empty evidence. There is nowhere in those return types to carry a reason.

## Sources

- In-repository contract, `CLAUDE.md` → "Derived evidence generations": "All
  graph, FTS, vector, tier, and telemetry generation state is disposable and
  derived." A disposable artifact failing to open is a degraded answer, not a
  failed request.
- In-repository contract, `CLAUDE.md` → "Agent integration boundary":
  "Automatic health context is injected only when `doctor` reports
  degraded/error findings." Reporting a damaged cache is doctor's job; a
  read-only structural query's job is to answer what it can and say what it
  could not.
- Python documentation,
  [`time.monotonic`](https://docs.python.org/3/library/time.html#time.monotonic)
  (fetched 2026-09-18): "Return the value (in fractional seconds) of a monotonic
  clock, i.e. a clock that cannot go backwards. The clock is not affected by
  system clock updates." The bound added to `_repository_freshness` is a
  monotonic deadline, the same kind `_active_evidence_graph` already takes.

## Decision

- `_repository_freshness` opens the generation under a 5 second monotonic
  deadline and answers `{"unavailable": <reason>}` in place of the freshness
  block when the generation cannot be read — `generation_unreadable:<Class>`
  from `GenerationUnreadable.reason`, or `deadline` when the bound is reached.
  The structural answer itself is unaffected and now says why its freshness is
  missing instead of silently dropping the field.
- `_open_navigation_graph` catches `GenerationUnreadable` and returns `None`,
  the degradation its three callers already implement. No reason is carried
  because none of the three return types has a place for one, and inventing one
  would change three answer shapes for a cache fault.
- Nothing else in `scripts/mcp_server.py` is touched; that file belongs to the
  navigation area and this is the smallest change its owner asked for.
