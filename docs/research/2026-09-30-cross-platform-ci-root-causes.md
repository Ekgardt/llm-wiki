# Cross-platform CI root causes — 2026-09-30

The actual CI failures remain recorded; local Python 3.12 success did not qualify Python 3.10 or macOS.

## Transaction initialization

CPython executescript commits a pending transaction before running its script. Therefore BEGIN outside executescript left schema migration checks and ALTER statements unprotected. Put BEGIN IMMEDIATE inside the trusted schema script, then run migrations and backfills in that same connection transaction. The existing connection context commits or rolls back. Catching duplicate-column errors would mask the race; moving schema creation outside the lock would preserve contention. No retry budget or schema change is introduced.

## Environment selection

Ask the selected environment only for its installed distribution inventory using stdlib importlib.metadata. Parse the project in the already provisioned caller and pass the inventory into the existing shared selection algorithm. Lazy TOML import makes inventory usable in a bare Python 3.10 environment. Installing TOML just to inspect an environment changes it; duplicating the selection algorithm creates divergent rules. Existing explicit preservation fallback stays available when introspection fails; no environment contract changes.

## Persisted owner identity

An absent bare PID does not prove that the recorded owner was local or that its process incarnation ended. Without a recorded birth identity return unknown on every platform. Keep scoped identity comparison and direct local PID probing distinct. Treating unknown as dead risks deleting protected state; keeping unknown requires an operator to resolve genuinely unidentified historical ownership.

Sources checked 2026-09-30: [CPython sqlite3](https://docs.python.org/3.10/library/sqlite3.html), [SQLite transactions](https://www.sqlite.org/lang_transaction.html), [Google SRE overload handling](https://sre.google/sre-book/handling-overload/), [CPython distribution metadata](https://docs.python.org/3.10/library/importlib.metadata.html), [uv exact synchronization](https://docs.astral.sh/uv/concepts/projects/sync/), [Microsoft process identifiers](https://learn.microsoft.com/en-us/windows/win32/procthread/process-handles-and-identifiers), [Linux PID namespaces](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html), [Apple getpid](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/getpid.2.html). These primary sources inform their respective mechanisms; the project regressions and CI logs establish the actual defects.

The capture refusal during an exclusive maintenance fence is a separate unresolved intake problem. These fixes do not establish its closure.
