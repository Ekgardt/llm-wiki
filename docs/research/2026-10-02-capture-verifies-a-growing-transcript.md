# Capture verifies retained bytes of a growing transcript

Research date: 2026-10-02. Supported Python baseline: 3.10; qualification here ran on Linux with the installed interpreter. No Windows execution is claimed.

A capture failure at 20:38:30 reported that a transcript changed while it was read. Its log does not retain the source path or before/after metadata, so the specific change is unknown. A five-second read-only observation of this agent's actual native rollout produced 59 reads and no failure. This does not establish that the historical failure was an append.

Deterministic reproductions independently establish two defects: ordinary appending during a read fails capture, and the large-transcript reader misses replacement of the pathname while its descriptor still names the old inode. A further protection test establishes that a same-size rewrite can escape a metadata-only check on this filesystem. Failed runs are retained in private qualification logs.

## Primary sources and alternatives

- [Python 3.10 os documentation](https://docs.python.org/3.10/library/os.html): descriptor reads, descriptor statistics, pathname statistics and no-follow availability. The implementation uses existing portable calls, not a new Python requirement.
- [Linux read(2)](https://man7.org/linux/man-pages/man2/read.2.html): reads can be short and operate at the descriptor offset; a retained range must be read completely rather than treating an early EOF as success.
- [JSON Lines specification](https://jsonlines.org/): records are newline-delimited. Existing complete-turn selection and token-boundary protection remain; plain-text and other admitted formats retain their previous rendering behavior.

Ignoring size changes cannot distinguish append from rewrite-plus-append. Repeated retries with delays cannot establish source identity or content and add unmeasured hook cost. Locking the file cannot coordinate native writers that do not participate in that lock. Copying a whole large transcript exceeds the measured capture resource contract. An OS-specific filesystem snapshot would change deployment requirements.

The selected internal reader fixes the initial open-file size, reads only the existing bounded whole-file or head/tail ranges, and compares those exact retained ranges with a second read. It checks regular-file status, safe ancestors, file identity, ownership, permissions, no shrink, and the final pathname identity. Growth may be accepted only while the retained bytes agree; later appended bytes belong to a subsequent capture. Same-size metadata changes, replacement, symlink redirection, shortened ranges and changed retained bytes remain refusals.

This is verified retained-range evidence, not an atomic snapshot of the omitted middle or a sandbox against hostile concurrent writers. The existing excerpt omission marker describes bytes omitted from the initial size. Small transcripts retain the entire initial byte range. Redaction and existing evidence budgets remain downstream. No new limit, path, schema, environment contract, actor or retry is introduced.

The second read adds bounded I/O. Its measured cost and related regression results belong in the audit evidence before installation. The generic strict authoritative-file reader remains unchanged: knowledge compilation must not reinterpret a changing Markdown file as a live conversation.

The replaced standalone excerpt and edge wrappers have no code consumers and are removed. Verified preimages and isolated qualification checkouts remain only for rollback and audit evidence; they are not parallel installed implementations.
