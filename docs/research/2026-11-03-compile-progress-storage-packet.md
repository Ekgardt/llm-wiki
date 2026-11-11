# Compile progress storage qualification

Date: 2026-11-03. The existing scripts/compile_progress.py is a 604-line owner of
append-only SQLite compile evidence, not the compiler orchestration or queue
worker. Qualify it in place without moving or changing production bytes. Keep its
existing journal tests in place and preserve their bytes. The complete original
2026-10-19 history remains intact and separately accounted through exact records.

Fresh built-in graph at 57a125bd was generation-18e472ef241e410d-cc7dccd4;
record-context-journal-graph.json records callers of append_event, audit_events and
completed_work as well as the context tools. Static observations were partial;
full source, journal tests and actual queue/controller/migration/health consumers
were inspected. Refresh after record infrastructure before new qualification code.

Production imports are sqlite_deadlines and the lazy formats.source descriptor.
It opens no database, starts no transaction, commits none and calls no model.
Callers own existing lease/payload/semantic fences and BEGIN IMMEDIATE. Exact
schema SQL, event digests, source-overlap reservations, artifact references,
retention and completion attestation remain unchanged. Source-interval uncertainty
uses existing metadata and no second registry. Compatibility exports and database
schema are unchanged; there is no new wrapper, runtime directory or install step.

Research checked 2026-11-03 from three independent primary publishers:
- SQLite atomic commit: https://www.sqlite.org/atomiccommit.html
- Python 3.10 sqlite3 transaction behavior:
  https://docs.python.org/3.10/library/sqlite3.html
- IETF JSON representation: https://www.rfc-editor.org/rfc/rfc8259
These support explicit caller transactions and exact retained JSON, not an external
provider exactly-once guarantee or a hostile-editor integrity claim. Python 3.10
remains the project's compatibility requirement. No dependency version changes.
Alternative extraction into a new storage facade adds indirection to an existing
independent owner; copying helpers duplicates authoritative behavior; including
all 77044 tokens of mixed historical orchestration in every storage prompt exceeds
the full-packet ceiling. Use the approved exact-record mechanism with complete
catalog and original byte reconstruction. Do not summarize required source.

The historical Markdown was inspected for fenced/indented code, blockquotes and
HTML blocks: none occur. Its ATX headings label real prose sections. The initial
storage/design/certification introduction forms one foundation record; subsequent
reviewed heading sections form records. The otherwise empty “Dependencies blocking
activation” heading belongs to its first adoption subsection, not an empty record.
This is a reviewed catalog for this exact source digest, not a general Markdown
parser or token-quota splitter. Every source byte must occur exactly once.

The storage packet needs the foundation and records for schema migration,
retention, durable call/repeat semantics, source uncertainty, work identity,
whole-work completion/queue settlement, operator summaries and fresh installation.
Catalog labels preserve the original dates/status and all unselected history stays
visible as unassigned records. Foundation is shared history context, not ownership
of the whole document. Catalog ownership is separate from original-source coverage.
Cross-component history records remain unassigned until their coherent owners are
qualified; their presence as shared context never silently claims them.

Before qualification, 332 related tests passed / 1 native-Windows skip across the
journal, admission, queue fencing, migration/cutover, health, repair, retention,
source overlap, model call/protected dispatch, selection, capture and fresh install.
Evidence: compile-progress-owner-baseline.txt and original source/test SHA-256 log.
This is a Linux regression baseline, not a deployment or model-cost measurement.
Next: exact catalog reconstruction and stale-source regression, isolated import
and full packet gates, existing journal/consumer checks and Python 3.10. No source
or original test mutation is planned; any discovered bug needs separate red proof.


## Candidate packet implementation

Fresh graph before qualification uses generation-18e474981d3b315b-96070b56 at
48cda3cd. append_event has four resolved callers, completed_work one and audit_events
two; partial graph status stays explicit and is supplemented by inspected source.
The first budget guard failed on the absent manifest while both existing isolation
guards passed. This is missing-metadata red evidence, not a production bug claim.

The catalog covers all 380666 original bytes in 86 reviewed records; concatenation
matches both the worktree and git-show of 57a125bd exactly (SHA-256
8db876e16e9f9b4a57e82129eae99bd78f960a5943aa98d7ffedd904f3323611).
The packet selects thirteen necessary records and owns only the coherent storage
foundation; 85 historical records remain unassigned. The original remains in the
unassigned-file inventory. Selection is by dated responsibility, not a token quota.
Both production source and original journal tests retain their baseline hashes.
The existing journal plus new boundary/budget tests pass 25 cases on Python 3.12
and Python 3.10. The full unchanged-production 57a125bd regression completed with
16115 passed / 244 skipped / 5 warnings; that is not a full run of new metadata.
All 68 emitted bundles round-trip, including selected exact record entries. Initial
storage packet size is 90740 tokens; final documentation is measured again before
commit. No historical file, SQL, class, function or test assertion was rewritten.

Global qualification completed: 1717 passed / 2 platform skips in 154.76 seconds.
After the separately reproduced whole-file/shared-record report correction, the
final ownership/measurement/storage checks passed 37 cases on Python 3.10. Actual
CCN/shape and Ruff checks pass. Inventory: 546 owned / 3269 tracked, 2723 unassigned;
partial historical coverage does not remove the original from that remainder.
