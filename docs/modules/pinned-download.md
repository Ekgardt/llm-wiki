# Pinned artifact transfer

`pinned_download.py` owns URL/status selection, no-proxy/no-redirect opener,
chunked transfer under a caller's absolute deadline, size/declared-length checks
and retry classification. Callers provide the destination writer and own partial
file truncation, artifact digest verification, installation locks and adoption.
The word pinned does not mean this layer itself verifies an artifact hash.

Every blocking read requires a usable socket timeout setter and read1 method;
missing response capabilities fail before an unbounded read. Redirect/status or
changed URL and invalid/incomplete length are refused. Runtime byte/chunk bounds
come from [bounded IO](bounded-io.md); existing archive bounds and retry waits keep
their current values and rationale. Retries cover transient network failures only
and must fit inside the same deadline. Permanent pin failures are not retried.

Independent existing retry/opener tests move here unchanged. Installer-specific
streaming, partial-file cleanup, lock and offline-component tests retain their
integration roles and are not replaced by leaf tests. No production path moves,
second implementation or network library is introduced.


## Preserved structure reference — 2026-11-03

`pinned_download.py` owns response selection, deadline-bounded streaming and
transient retry. Installers retain integrity verification, partial-file cleanup,
locks and adoption.
