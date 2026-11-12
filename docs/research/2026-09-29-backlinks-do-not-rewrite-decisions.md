# Backlinks do not rewrite decisions

Research date: 2026-09-29. The current operating contract says decisions are
immutable and must be superseded instead of edited in place. Structural lint
nevertheless requires a reciprocal link in active decisions, and the repair
writer appends one. Only retired pages were exempt. The installed post-compile
lint found 18 missing backlinks; four target decision pages. No automated repair
was applied to those pages during this investigation.

An older private memory pattern requires reciprocal links even in decisions.
That conflicts with the current user-supplied canonical contract. The current
instruction controls; the old memory must retain its history with the conflict
recorded. This is not permission to change a decision's contents or to disable
the ordinary-note backlink check.

## Current research and choice

Three independent primary sources were checked:

- [AWS ADR process](https://docs.aws.amazon.com/prescriptive-guidance/latest/architectural-decision-records/adr-process.html)
  preserves accepted/rejected decisions and uses new records for changes.
- [Microsoft Well-Architected ADR guidance](https://learn.microsoft.com/en-us/azure/well-architected/architect-role/architecture-decision-record),
  updated 2026-04-13, treats the log as append-only and explicitly says not to
  edit accepted records. Superseding records preserve the decision history.
- [Obsidian backlinks](https://help.obsidian.md/plugins/backlinks) displays linked
  mentions derived from other notes. Discovering an incoming link need not add
  a literal link to the referenced source document.

The best fit is to preserve decision bytes, stop demanding a physical reciprocal
link in them, and enforce the same rule immediately before the repair transaction.
The original forward link remains intact. Ordinary mutable notes still owe their
existing reciprocal links. Archived/superseded pages remain protected. Parse
metadata with the existing common YAML reader, including quoted types and inline
comments; unreadable metadata must not grant write permission.

Editing an accepted decision to satisfy lint violates the governing contract.
Removing the original forward links loses real relationships. Suppressing all
backlink findings would hide ordinary-note defects. Adding another backlink
database or changing the knowledge layout is unnecessary. The tradeoff is that
an immutable decision does not acquire literal reverse links to later notes;
the incoming source link remains evidence of the relationship.

No new library, schema, path, environment variable, runtime location, limit, or
model call is introduced. The common reader already uses the installed PyYAML
dependency and the correction keeps Python 3.10-compatible syntax. This repair
does not add a new navigation UI or claim to repair every historical decision
that previous runs may have edited.

## Qualification

Before the correction, three active-decision cases (plain type, quoted type,
inline YAML comment) and a target changed into a decision between planning and
execution fail. The original transaction test is retained with a mutable concept
as its target; its assertions still prove the backlink is written. The repair
must leave protected targets byte-identical, keep the source link, refuse a
newly protected target at the write boundary, and retain hash preconditions for
edits after the last read.

The four new regressions failed before the correction. Afterwards, 19 focused
tests, 44 lint/status/Python-complexity checks, and 36 related consumer tests
passed. The installed repair then added 14 owed backlinks with zero failures;
the protected decisions were not repair targets. A subsequent full structural
lint exited successfully, with three nonblocking orphan daily logs still named.
This does not establish that historical edits to decisions have been undone.
