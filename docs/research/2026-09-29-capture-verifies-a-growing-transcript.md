# Capture verifies a growing transcript

Date: 2026-09-29. Scope: local lifecycle transcript reads; no storage, host,
dependency, retention, or runtime-location contract changes.

## Evidence and path

Native session-end and pre-compact events publish a durable intent through
`ingest_event`, `_publish_durable_capture_intent`, `_fitting_capture_record`,
and `_capture_path_evidence`. A transcript read failure happens before durable
publication. The reader previously rejected even an append after the first
read. Deterministic tests reproduced that failure for both whole small files
and excerpts of large files. The historical pre-compact error has the same
message, but its log lacks before/after metadata: its exact mutation remains
unknown. This change does not establish that all historical captures survived.

## Sources and alternatives

Three independent primary references consulted on this date:

- [Python os documentation](https://docs.python.org/3/library/os.html): descriptor
  reads, seeks and metadata; used by the existing Python implementation.
- [Linux read(2)](https://www.man7.org/linux/man-pages/man2/read.2.html): successful
  reads can be shorter than requested; offsets advance by bytes actually read.
- [Microsoft FileStream.Read](https://learn.microsoft.com/en-us/dotnet/api/system.io.filestream.read?view=net-10.0):
  the same short-read and EOF distinction on a second supported platform.

These sources establish I/O semantics, not an atomic snapshot guarantee. The
algorithm's narrower acceptance property is exercised by project tests. No
new technology or dependency is introduced. Existing bounded chunk reads and
regular-file/path checks remain necessary on supported hosts.

Rejected alternatives: ignoring changed metadata accepts rewritten evidence;
raising size/time limits does not distinguish append from rewrite; repeated
retries add unpredictable hook latency; copying entire large transcripts adds
unnecessary I/O and still needs consistency checks; advisory locking cannot
constrain host writers that do not participate in the lock protocol.

## Decision and tradeoff

Freeze the requested windows and size when opening the file. Read those windows
through the existing bounded chunk reader. Metadata changes must indicate
growth of the same file. Read the same windows once more, requiring unchanged
metadata during that verification and byte-for-byte agreement. Verify that the
path still identifies the opened regular file. Reject truncation, replacement,
rewritten captured bytes and observed mutation during verification.

Verification runs even when initial metadata matches: a fast same-size rewrite
can share the same observable timestamp. A regression exposed this locally.
The cost is a second bounded read, not a second whole-file copy for excerpts.
Later appended bytes are outside this capture's cutoff and remain available
to later lifecycle events. Existing redaction and explicit excerpt-gap reporting
are unchanged. This is a verified captured window, not a promise that omitted
middle bytes or a file after the final check never change.

## Acceptance and remaining scope

Tests cover small and large appends, rewritten bytes with growth, truncation,
same-size rewrites, path replacement, continued growth during verification,
and durable publication for pre-compact and session-end. Existing evidence
packing, secret redaction and lifecycle tests cover downstream consumers.
The common reader replaces the separate small/large opening implementations;
the strict general-purpose bounded reader remains needed by authoritative
non-transcript consumers. No parallel legacy transcript reader is retained.

Breadcrumb append deadlines, pending compile work and quarantined transactions
are separate unresolved audit items; this change does not claim to fix them.
