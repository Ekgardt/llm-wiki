# A default budget for a dead-code answer

Date: 2026-08-29. Rule-2 research for the change that gives
`find_dead_code` a default answer budget and an order that decides what a cut
drops.

## The condition, measured

Measured on this vault today, `find_dead_code` on the repository root, through
the stored generation:

| | |
|---|---|
| candidates | 873 |
| raw answer | 254,150 characters — 63,537 estimated tokens |
| after `answer_budget` drops the opaque ids (today's default) | 202,695 characters — **50,673 estimated tokens** |
| `zero_confirmed_incoming_calls` | 461 |
| `unresolved_receiver` | 412 |
| answer frame with no candidates | 63 tokens |

The tool accepts `budget_tokens` and applies none by default, so the routine
question costs 50,673 tokens — **2.0x the 25,000-token client tool-result
ceiling `answer_budget.MAX_BUDGET_TOKENS` already documents**. Above that
ceiling the host cuts, and the module's own docstring records why that is the
bad outcome: MCP defines no field for "this was truncated", so a host-side cut
reaches the reader as a complete answer that is not one.

## What practice says

There is still no size limit and no truncation signal in the protocol. In
`modelcontextprotocol` discussion #2211 a maintainer (Clare Liguori, 2026-02-11)
suggests out-of-band access for oversized content, and a collaborator
(SamMorrowDrums, 2026-02-15) notes that "clients can also truncate or summarise
large responses too" — i.e. the burden today sits on client implementations,
each with its own undisclosed threshold. Server-side, the discussion converges
on pagination, disk spillover, and capability negotiation.

The general recommendation in current MCP practice is a bounded default plus the
full count: return a top-N by default, include `total_count`, include a cursor.
The named failure mode is *silent* truncation — an agent that answers
confidently from a result the host quietly cut.

This product has the first two halves and not the third. It bounds at the
source, and it reports `truncated` and `rows_omitted`. It has no cursor, and
adding one is an MCP input-schema change in `scripts/mcp_server.py`, which this
change does not touch. That gap is stated, not closed: a reader who needs the
omitted rows has no paged way to ask for them, only a larger `budget_tokens`.

Dead-code tools order by confidence as a matter of course. Vulture scores each
finding 60–100% and ships `--min-confidence` so a reader can ask for the certain
ones first. This product already carries the same distinction under a different
name — `_dead_code_reason` separates "nothing names it anywhere" from "some call
text names it, so a dynamic call may reach it" — but never let it decide
anything.

## The two decisions, and the measurement behind each

### 1. The default is 25,000 tokens — `MAX_BUDGET_TOKENS`

Not taste: it is the only value in the served range at which the cut falls
entirely outside what the answer claims. Measured, on the same 873 candidates,
with the defensible reason ordered first:

| default | rows kept | rows dropped | defensible rows dropped |
|---|---|---|---|
| 25,000 | 519 | 354 | **0** |
| 12,000 | 245 | 628 | 216 |
| 8,000 | 163 | 710 | 298 |
| 4,000 | 77 | 796 | 384 |

At 25,000 the ladder drops `owner` (derivable from `path`) before any row, and
that alone buys room for all 461 `zero_confirmed_incoming_calls` rows plus 58 of
the doubtful ones. Every smaller default starts deleting the part of the answer
the tool actually asserts.

The ceiling is also the largest number with anything behind it: a default above
it would be a budget the client cannot carry.

Blast radius, measured: `shape_code_answer` is reached by exactly two tools.
`get_architecture` on this repository costs 19,979 tokens, under the default, so
it is unchanged except for the report block (19,991 tokens, +12). The default
only bites answers that already exceed the deliverable ceiling — answers that are
being cut today, silently, by the host.

### 2. The defensible reason is ordered first

The ladder drops rows from the tail. The tail of an alphabetically sorted list is
an arbitrary alphabetical slice, so today's order decides nothing and loses the
answer's content at random. Measured at the same 25,000-token budget, same rows,
order the only difference:

| order | kept | `zero_confirmed_incoming_calls` lost | `unresolved_receiver` kept |
|---|---|---|---|
| by `(name, file, line)` — today | 526 | **97** | 162 |
| by reason, then `(name, file, line)` | 519 | **0** | 58 |

97 defensible candidates were being thrown away to keep 162 rows about which the
tool declines to claim anything. `unresolved_receiver` means a call site names
that symbol and the receiver could not be resolved — the row exists to state
doubt, and doubt is what a cut should drop first.

Ordering is not filtering. Nothing is excluded: with a large enough budget every
row is still returned, and the report still says how many were dropped.

### 3. The counts survive any cut

A cut answer must not misreport the shape of the problem. `candidate_count` and
`candidates_by_reason` go in the report block, which the budget never trims, so
"461 defensible, 412 doubtful" reaches the reader even when 354 rows do not.
This is the `total_count` half of the practice recommendation, and it costs
about 30 tokens.

## Rejected

**A smaller, frugal default (8,000–12,000).** Rule 4 asks for token thrift, but
measured above, every value below the ceiling deletes rows the tool asserts are
dead. Thrift that removes the answer is not thrift; a caller who wants a cheaper
answer already has `budget_tokens`.

**Bounding the row count in `code_graph`, like `COMMUNITY_LIMIT`.** That
precedent exists a few hundred lines away in the same file and was the first
candidate. It is right for communities, whose members are bare ids that no
budget ladder could shorten usefully; it is wrong here, because a fixed row count
cannot know what a row costs, and `answer_budget` already implements the exact
ladder — hash, then derivable field, then tail row — with the `truncated` and
`rows_omitted` reporting this needs.

**Filtering `unresolved_receiver` out by default.** It would make the answer
small and would be a silent cut of a whole class, which is the thing the module
exists to prevent. Ordered last, those rows are dropped only when the budget
forces it, and the report says how many.

## Sources

- [Response size limit for MCP responses to prevent context overflow in AI Agents · modelcontextprotocol discussion #2211](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/2211)
- [Tool-Result Truncation: The Silent Bug That Makes Agents Lie](https://dev.to/gabrielanhaia/tool-result-truncation-the-silent-bug-that-makes-agents-lie-3epe)
- [MCP Optimization Best Practices — Nick Babich, July 2026](https://uxplanet.org/mcp-optimization-best-practices-181dc750a790)
- [Pagination — FastMCP](https://gofastmcp.com/servers/pagination)
- [vulture — find dead Python code](https://github.com/jendrikseipp/vulture)
